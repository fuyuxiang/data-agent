"""Unified metric/field lineage (S4 P1-05).

Single entry point for resolving metric dependencies to physical columns.
Replaces the two previous implementations that drifted apart:

- `compiler/metrics.resolve_metric_dependencies` used sqlglot AST
- `security/columns._metric_field_names` used string split

Drift between those two was a real permission leak (P1-05). Both call sites
now consume this module.
"""

from __future__ import annotations

import sqlglot
from dataclasses import dataclass
from sqlglot import exp

from app.semantic.model import DatasetDef, MetricDef


@dataclass(frozen=True)
class MetricNode:
    """A metric and its dependencies in the lineage graph."""

    name: str
    kind: str
    source_field: str | None
    expression: str
    dependencies: tuple[str, ...]  # Names of metrics this depends on


@dataclass(frozen=True)
class MetricDag:
    """The metric dependency graph for one dataset.

    Built once per dataset; lookup is by metric name.
    """

    nodes: tuple[MetricNode, ...]

    def dependencies(self, metric_name: str) -> tuple[str, ...]:
        """Get direct dependencies for a metric."""
        for node in self.nodes:
            if node.name == metric_name:
                return node.dependencies
        return ()

    def all_dependencies(self, metric_name: str) -> frozenset[str]:
        """Get all transitive dependencies (BFS)."""
        result: set[str] = set()
        queue = [metric_name]
        seen: set[str] = set()
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            deps = self.dependencies(name)
            result.update(deps)
            queue.extend(deps)
        return frozenset(result)


def _parse_expression_dependencies(
    expression: str,
    known_metrics: set[str],
) -> tuple[str, ...]:
    """Parse a metric expression AST to find referenced metric names.

    Uses sqlglot for accuracy: handles quotes, escape sequences, and complex
    expressions. Same logic as the original compiler/metrics.py.
    """
    if not expression:
        return ()

    try:
        tree = sqlglot.parse_one(expression, dialect="postgres")
    except Exception:
        return ()

    resolved: list[str] = []
    seen: set[str] = set()
    for column in tree.find_all(exp.Column):
        name = column.name
        if name in known_metrics and name not in seen:
            seen.add(name)
            resolved.append(name)
    return tuple(resolved)


def metric_dag(dataset: DatasetDef) -> MetricDag:
    """Build the metric DAG for a dataset.

    Each node captures a metric's direct dependencies (other metric names
    appearing in its expression). Use ``all_dependencies()`` for transitive.
    """
    known_metrics = {m.name for m in dataset.metrics}
    nodes: list[MetricNode] = []
    for metric in dataset.metrics:
        deps = _parse_expression_dependencies(metric.expression, known_metrics)
        # Exclude self-reference; resolve_metric_dependencies already filters
        # this but we apply it here as a safety net.
        deps = tuple(d for d in deps if d != metric.name)
        nodes.append(
            MetricNode(
                name=metric.name,
                kind=metric.kind,
                source_field=metric.source_field,
                expression=metric.expression,
                dependencies=deps,
            )
        )
    return MetricDag(nodes=tuple(nodes))


def _field_lineage_recursive(
    metric: MetricDef,
    dag: MetricDag,
    seen: set[str],
) -> set[str]:
    """Recursively collect physical column names a metric reads."""
    if metric.name in seen:
        return set()
    seen.add(metric.name)

    names: set[str] = set()
    if metric.source_field:
        names.add(metric.source_field)

    # Recurse into dependencies
    for dep_name in dag.dependencies(metric.name):
        if dep_name not in seen:
            from app.semantic.model import DatasetDef
            # Re-resolve the metric (caller-provided dataset; we look it up
            # via dag indirectly by walking the original dataset).
            # This is a re-resolve path; for callers that only have DatasetDef
            # they can use field_lineage() directly.
            names.update(_field_lineage_by_name(dep_name, dag, seen))

    return names


def _field_lineage_by_name(
    metric_name: str,
    dag: MetricDag,
    seen: set[str],
) -> set[str]:
    """Resolve field lineage for a metric by name (requires dataset)."""
    for node in dag.nodes:
        if node.name == metric_name:
            names: set[str] = set()
            if node.source_field:
                names.add(node.source_field)
            for dep_name in node.dependencies:
                names.update(_field_lineage_by_name(dep_name, dag, seen))
            return names
    return set()


def field_lineage(dataset: DatasetDef, metric_name: str) -> frozenset[str]:
    """Resolve the set of physical field names a metric ultimately reads.

    Replaces the previous two implementations:
    - `compiler/metrics.resolve_metric_dependencies` (AST-based)
    - `security/columns._metric_field_names` (string-split based)

    Both are now thin wrappers around this function to keep the call sites
    backwards compatible.
    """
    if not dataset.has_metric(metric_name):
        return frozenset()

    dag = metric_dag(dataset)
    seen: set[str] = set()
    result = _field_lineage_by_name(metric_name, dag, seen)
    return frozenset(result)


def resolve_metric_dependencies(
    dataset: DatasetDef, metric: MetricDef
) -> list[MetricDef]:
    """Backwards-compatible wrapper for `compiler/metrics.resolve_metric_dependencies`.

    Returns a list of MetricDef (atomic metrics) that the given composite/ratio
    metric depends on. Matches the signature of the previous implementation.
    """
    if metric.kind not in ("composite", "ratio"):
        return []

    dag = metric_dag(dataset)
    seen: set[str] = set()
    resolved: list[MetricDef] = []
    queue: list[str] = list(dag.dependencies(metric.name))
    while queue:
        dep_name = queue.pop(0)
        if dep_name in seen or dep_name == metric.name:
            continue
        seen.add(dep_name)
        if dataset.has_metric(dep_name):
            dep_metric = dataset.metric(dep_name)
            resolved.append(dep_metric)
            # Recurse into composite/ratio dependencies
            if dep_metric.kind in ("composite", "ratio"):
                queue.extend(dag.dependencies(dep_name))
    return resolved
