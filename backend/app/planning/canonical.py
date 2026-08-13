"""Canonical Query Plan — the stable, cacheable, permission-free semantic plan.

This is the S3 contract (spec §3.1) that sits between intent recognition and
policy compilation. It contains the fully resolved time range, typed filters,
declared physical field lineage, and multi-metric time basis — everything
needed to deterministically compile to SQL, but nothing about the principal,
principal's roles, or row/column policies.

This split is what makes Trusted Asset reuse safe: the canonical plan can be
re-used across users and tenants, while the secured plan (which does have
principal and policy info) cannot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import Any, Optional


# Plan version — bump when the structure changes in a way that affects hash.
PLAN_VERSION = "1.0.0"


class TimeBasisKind(str, Enum):
    """Unit of the time window for a measure."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


@dataclass(frozen=True)
class TimeExpression:
    """Original user time expression, preserved for VQ re-resolve."""

    kind: str  # "relative" | "absolute" | "range" | "none"
    expression: str  # user phrasing
    unit: Optional[str] = None
    offset: int = 0
    to_date: bool = False
    start_date: Optional[date] = None
    end_date: Optional[date] = None


@dataclass(frozen=True)
class ResolvedTimeRange:
    """Resolved time range, with the original expression preserved."""

    expression: TimeExpression
    start: date
    end: date
    grain: TimeBasisKind
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Measure:
    """A metric with its identifying information and time basis."""

    metric_name: str
    version: int
    time_basis: str  # physical column name for time
    alias: Optional[str] = None


@dataclass(frozen=True)
class TypedFilter:
    """A filter with resolved literal values."""

    field: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalQueryPlan:
    """The canonical, stable, permission-free plan contract (S3 spec §3.1).

    Key properties:
    - Frozen: hashable, sharable, deterministic
    - Self-contained: includes everything needed to compile SQL
    - Permission-free: no principal, roles, or row/column policies
    - Field lineage: explicit declare of physical columns read
    """

    plan_version: str
    semantic_revision_id: int
    domain: str
    dataset: str
    measures: tuple[Measure, ...]
    group_by: tuple[str, ...]
    typed_filters: tuple[TypedFilter, ...]
    resolved_time_range: Optional[ResolvedTimeRange]
    comparison: Optional[str]
    sort: Optional[dict[str, Any]]
    pagination: Optional[int]
    required_field_lineage: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    clarifications: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Stable serialisation with deterministic field ordering.

        Used for hashing and JSON storage. Field order is critical for stability.
        """
        d = {
            "plan_version": self.plan_version,
            "semantic_revision_id": self.semantic_revision_id,
            "domain": self.domain,
            "dataset": self.dataset,
            "measures": [
                {
                    "metric_name": m.metric_name,
                    "version": m.version,
                    "time_basis": m.time_basis,
                    "alias": m.alias,
                }
                for m in self.measures
            ],
            "group_by": sorted(self.group_by),
            "typed_filters": [
                {"field": f.field, "operator": f.operator, "values": sorted(f.values)}
                for f in sorted(self.typed_filters, key=lambda x: (x.field, x.operator))
            ],
            "resolved_time_range": (
                None
                if self.resolved_time_range is None
                else {
                    "expression": {
                        "kind": self.resolved_time_range.expression.kind,
                        "expression": self.resolved_time_range.expression.expression,
                        "unit": self.resolved_time_range.expression.unit,
                        "offset": self.resolved_time_range.expression.offset,
                        "to_date": self.resolved_time_range.expression.to_date,
                        "start_date": (
                            self.resolved_time_range.expression.start_date.isoformat()
                            if self.resolved_time_range.expression.start_date
                            else None
                        ),
                        "end_date": (
                            self.resolved_time_range.expression.end_date.isoformat()
                            if self.resolved_time_range.expression.end_date
                            else None
                        ),
                    },
                    "start": self.resolved_time_range.start.isoformat(),
                    "end": self.resolved_time_range.end.isoformat(),
                    "grain": self.resolved_time_range.grain.value,
                    "assumptions": sorted(self.resolved_time_range.assumptions),
                }
            ),
            "comparison": self.comparison,
            "sort": self.sort,
            "pagination": self.pagination,
            "required_field_lineage": sorted(self.required_field_lineage),
            "assumptions": sorted(self.assumptions),
            "clarifications": sorted(self.clarifications),
        }
        return d

    def hash(self) -> str:
        """Deterministic hash of the canonical plan, used for cache key.

        Sort fields and use compact JSON to ensure byte-stable output.
        """
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ColumnAccess:
    """Column-level policy decision (used in Secured Plan)."""

    field: str
    access: str  # "allow" | "mask" | "deny"


@dataclass(frozen=True)
class RowPredicate:
    """Compiled row-level filter (RLS)."""

    field: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class SecuredExecutionPlan:
    """Secured execution plan: canonical + principal + policy.

    Built by compile_policy() from a Canonical Query Plan plus the principal
    and policy revision. Never cached, never shared across principals.
    """

    canonical: CanonicalQueryPlan
    principal_id: int
    tenant_id: str
    policy_revision: int
    policy_hash: str
    row_predicates: tuple[RowPredicate, ...]
    column_decisions: tuple[ColumnAccess, ...]
    dialect: str
    warehouse_profile: str
    execution_budget: int

    def hash(self) -> str:
        """Hash including principal + policy, so cache keys don't leak across users."""
        payload = {
            "canonical_hash": self.canonical.hash(),
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "policy_revision": self.policy_revision,
            "policy_hash": self.policy_hash,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class InconsistentTimeBasisError(Exception):
    """Raised when multiple measures have different time bases."""

    def __init__(self, metric_a: str, basis_a: str, metric_b: str, basis_b: str):
        self.metric_a = metric_a
        self.basis_a = basis_a
        self.metric_b = metric_b
        self.basis_b = basis_b
        super().__init__(
            f"Metrics {metric_a} (time_basis={basis_a}) and "
            f"{metric_b} (time_basis={basis_b}) have different time bases. "
            f"Use '分别统计' to compute them separately."
        )
