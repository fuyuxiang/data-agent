"""Security rewrites on the compiled AST.

Row policies are injected by walking every SELECT node in the tree, not just
the outermost one: a comparison query is two CTEs, and restricting only the
outer SELECT would leave the baseline period fully readable.
"""

from dataclasses import dataclass

from sqlglot import exp

from app.security.principal import Principal, RowRule
from app.semantic.model import DatasetDef


class RowPolicyConfigError(Exception):
    """A policy references something the dataset no longer has.

    Raised instead of skipping the policy: failing closed is the only safe
    behaviour when the alternative is silently granting wider access.
    """


@dataclass(frozen=True, slots=True)
class AppliedRowFilter:
    """What the citation block shows as 「由数据权限自动附加」."""

    field_business_name: str
    values: tuple[str, ...]


def _policy_predicate(dataset: DatasetDef, rule: RowRule) -> exp.Expression:
    if not dataset.has_field(rule.field_name):
        raise RowPolicyConfigError(
            f"行权限策略引用了数据集 {dataset.name} 中不存在的字段 {rule.field_name}"
        )
    field = dataset.field(rule.field_name)
    column = exp.column(field.physical_column)
    literals = [exp.Literal.string(value) for value in rule.values]

    if rule.operator == "not_in":
        return exp.Not(this=exp.In(this=column, expressions=literals))
    return exp.In(this=column, expressions=literals)


def _business_values(dataset: DatasetDef, rule: RowRule) -> tuple[str, ...]:
    """Physical policy values rendered for display.

    Values without a dictionary entry are shown as-is rather than dropped —
    an incomplete permission line is worse than an unpolished one.
    """
    field = dataset.field(rule.field_name)
    labels: list[str] = []
    for value in rule.values:
        match = next(
            (item for item in field.enum_values if item.physical_value == value),
            None,
        )
        labels.append(match.business_value if match else value)
    return tuple(labels)


def inject_row_policies(
    ast: exp.Expression, dataset: DatasetDef, principal: Principal
) -> tuple[exp.Expression, tuple[AppliedRowFilter, ...]]:
    rules = principal.row_rules_for(dataset.name)
    if not rules:
        return ast, ()

    predicates = [_policy_predicate(dataset, rule) for rule in rules]
    applied = tuple(
        AppliedRowFilter(
            field_business_name=dataset.field(rule.field_name).business_name or rule.field_name,
            values=_business_values(dataset, rule),
        )
        for rule in rules
    )

    rewritten = ast.copy()
    for select in rewritten.find_all(exp.Select):
        # Only SELECTs reading the physical table need the restriction; the
        # outer SELECT of a comparison query reads CTEs, whose sources are
        # already restricted.
        if not _reads_physical_table(select, dataset):
            continue
        for predicate in predicates:
            select.where(predicate.copy(), copy=False)

    return rewritten, applied


def _reads_physical_table(select: exp.Select, dataset: DatasetDef) -> bool:
    table_name = dataset.physical_table.split(".")[-1]
    return any(
        isinstance(source, exp.Table) and source.name == table_name
        for source in select.find_all(exp.Table)
    )