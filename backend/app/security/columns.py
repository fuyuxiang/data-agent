"""Column permissions and masking.

Two distinct mechanisms:

- DENY removes the field from the semantic view entirely, so it is invisible at
  recall time — the model is never told a forbidden field exists.
- MASK keeps the field queryable but replaces its value in the projection.

Filtering on a masked field is refused: the value would be recoverable by
probing which filters return rows.
"""

from dataclasses import replace

from sqlglot import exp

from app.intent.schema import QueryIntent
from app.security.principal import ColumnAccess, Principal
from app.semantic.model import DatasetDef, MetricDef

_MASK_LITERAL = "***"


class PermissionDeniedError(Exception):
    """Fixed message: revealing which object was denied confirms it exists."""

    def __init__(self) -> None:
        super().__init__("你没有该数据的访问权限")


def _access(dataset: DatasetDef, principal: Principal, field_name: str) -> ColumnAccess:
    if not dataset.has_field(field_name):
        return ColumnAccess.ALLOW
    return principal.column_access(dataset.field(field_name), dataset.name)


def _metric_field_names(dataset: DatasetDef, metric: MetricDef, seen: set[str]) -> set[str]:
    """Fields a metric ultimately reads, following metric references."""
    if metric.name in seen:
        return set()
    seen.add(metric.name)

    names: set[str] = set()
    if metric.source_field:
        names.add(metric.source_field)
    if metric.expression:
        for token in metric.expression.replace("(", " ").replace(")", " ").split():
            if dataset.has_metric(token):
                names |= _metric_field_names(dataset, dataset.metric(token), seen)
            elif dataset.has_field(token):
                names.add(token)
    return names


def visible_dataset(dataset: DatasetDef, principal: Principal) -> DatasetDef:
    denied = {
        field.name
        for field in dataset.fields
        if principal.column_access(field, dataset.name) == ColumnAccess.DENY
    }
    if not denied:
        return dataset

    fields = tuple(field for field in dataset.fields if field.name not in denied)
    metrics = tuple(
        metric
        for metric in dataset.metrics
        if not (_metric_field_names(dataset, metric, set()) & denied)
    )
    return replace(dataset, fields=fields, metrics=metrics)


def assert_intent_permitted(
    dataset: DatasetDef, intent: QueryIntent, principal: Principal
) -> None:
    for metric_name in intent.metrics:
        if not dataset.has_metric(metric_name):
            continue
        metric = dataset.metric(metric_name)
        for field_name in _metric_field_names(dataset, metric, set()):
            if _access(dataset, principal, field_name) == ColumnAccess.DENY:
                raise PermissionDeniedError

    for name in intent.dimensions:
        if _access(dataset, principal, name) == ColumnAccess.DENY:
            raise PermissionDeniedError

    for condition in intent.filters:
        # MASK also blocks filtering: a permitted filter would leak the value.
        if _access(dataset, principal, condition.field) != ColumnAccess.ALLOW:
            raise PermissionDeniedError


def apply_masking(
    ast: exp.Expression, dataset: DatasetDef, principal: Principal
) -> tuple[exp.Expression, tuple[str, ...]]:
    masked_columns = {
        field.physical_column: field
        for field in dataset.fields
        if principal.column_access(field, dataset.name) == ColumnAccess.MASK
    }
    if not masked_columns:
        return ast, ()

    hit: dict[str, str] = {}
    rewritten = ast.copy()

    for alias in rewritten.find_all(exp.Alias):
        inner = alias.this
        if isinstance(inner, exp.Column) and inner.name in masked_columns:
            field = masked_columns[inner.name]
            hit[field.name] = field.business_name or field.name
            # Keep the alias so downstream column mapping is unaffected.
            alias.set("this", exp.Literal.string(_MASK_LITERAL))

    return rewritten, tuple(hit[name] for name in sorted(hit))