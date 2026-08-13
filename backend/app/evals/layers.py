"""Structured 7-layer evaluation (S5).

A green light means correctness, not just "the test ran". Each layer
produces its own outcome (PASS / FAIL / SKIPPED) and a structured report.
The final evaluation is the AND of all layers' PASS outcomes; a single
FAIL at any layer fails the case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class LayerOutcome(str, Enum):
    """Per-layer outcome.

    PASS:    assertions held
    FAIL:    assertions broken (case fails overall)
    SKIPPED: layer not exercised (e.g. no intent to compare); does not
             fail the case, but is reported in stats so missing
             assertions are visible
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


class Tolerance(str, Enum):
    """Field-level tolerance for differences.

    STRICT:  any difference → FAIL the case
    LENIENT: differences recorded but do not fail the case
    """

    STRICT = "strict"
    LENIENT = "lenient"


# Field categories that must FAIL on any real-model difference, per S5 spec.
STRICT_FIELDS = frozenset({"metrics", "time", "permissions", "status"})

# Fields that may be compared by semantic equivalence (e.g., region aliases).
LENIENT_FIELDS = frozenset({"dimensions", "filters"})


@dataclass(frozen=True)
class FieldDiff:
    """A single field-level diff between expected and actual."""

    field: str
    expected: Any
    actual: Any
    is_strict: bool
    is_critical_diff: bool  # True if this should FAIL the case

    def describe(self) -> str:
        return f"{self.field}: expected={self.expected!r} actual={self.actual!r}"


@dataclass(frozen=True)
class LayerReport:
    """Per-layer evaluation result."""

    layer: str
    outcome: LayerOutcome
    diffs: tuple[FieldDiff, ...] = ()
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == LayerOutcome.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "outcome": self.outcome.value,
            "diffs": [d.describe() for d in self.diffs],
            "message": self.message,
        }


# --- Layer implementations -------------------------------------------------


def evaluate_intent_layer(
    expected_intent: dict | None,
    actual_intent: dict | None,
    tolerance: Tolerance = Tolerance.STRICT,
) -> LayerReport:
    """Layer 1: Intent slot comparison.

    Compares intent slot-by-slot. Strict fields (metrics, time) FAIL on
    any difference. Lenient fields (dimensions, filters) are compared
    by semantic equivalence.

    Args:
        expected_intent: expected intent dict
        actual_intent: actual intent dict
        tolerance: STRICT (any diff → FAIL) or LENIENT (diffs recorded but OK)
    """
    if expected_intent is None and actual_intent is None:
        return LayerReport(layer="intent", outcome=LayerOutcome.SKIPPED)
    if expected_intent is None or actual_intent is None:
        # One side missing intent
        if tolerance == Tolerance.LENIENT:
            return LayerReport(
                layer="intent",
                outcome=LayerOutcome.PASS,
                message="One side has intent, the other does not (lenient mode)",
            )
        return LayerReport(
            layer="intent",
            outcome=LayerOutcome.FAIL,
            message="One side has intent, the other does not",
        )

    diffs: list[FieldDiff] = []
    for field_name, expected_value in expected_intent.items():
        actual_value = actual_intent.get(field_name)
        is_strict = field_name in STRICT_FIELDS
        if field_name in LENIENT_FIELDS:
            # Semantic equivalence: same SET, not same representation
            is_critical = not _semantic_equivalent(expected_value, actual_value)
        else:
            is_critical = expected_value != actual_value
        if is_critical:
            diffs.append(
                FieldDiff(
                    field=field_name,
                    expected=expected_value,
                    actual=actual_value,
                    is_strict=is_strict,
                    is_critical_diff=True,
                )
            )

    if diffs:
        if tolerance == Tolerance.LENIENT:
            # Record diffs but don't fail
            return LayerReport(
                layer="intent",
                outcome=LayerOutcome.PASS,
                diffs=tuple(diffs),
                message=f"{len(diffs)} intent field(s) differ (lenient mode: recorded but OK)",
            )
        return LayerReport(
            layer="intent",
            outcome=LayerOutcome.FAIL,
            diffs=tuple(diffs),
            message=f"{len(diffs)} intent field(s) differ",
        )
    return LayerReport(layer="intent", outcome=LayerOutcome.PASS)


def evaluate_status_layer(
    expected_status: str | None, actual_status: str | None
) -> LayerReport:
    """Layer 2: Final answer status (answered / clarifying / refused / failed)."""
    if expected_status is None or actual_status is None:
        return LayerReport(layer="status", outcome=LayerOutcome.SKIPPED)
    if expected_status == actual_status:
        return LayerReport(layer="status", outcome=LayerOutcome.PASS)
    return LayerReport(
        layer="status",
        outcome=LayerOutcome.FAIL,
        diffs=(FieldDiff(
            field="status",
            expected=expected_status,
            actual=actual_status,
            is_strict=True,
            is_critical_diff=True,
        ),),
        message=f"Status mismatch: {expected_status} vs {actual_status}",
    )


def evaluate_sql_layer(
    expected_sql: str | None, actual_sql: str | None
) -> LayerReport:
    """Layer 3: Compiled SQL comparison (after canonical normalisation)."""
    if expected_sql is None or actual_sql is None:
        return LayerReport(layer="sql", outcome=LayerOutcome.SKIPPED)
    # For now, byte-equality is the strict check. Spec 3.3 also says
    # byte-equality on the same plan+revision.
    if expected_sql.strip() == actual_sql.strip():
        return LayerReport(layer="sql", outcome=LayerOutcome.PASS)
    return LayerReport(
        layer="sql",
        outcome=LayerOutcome.FAIL,
        diffs=(FieldDiff(
            field="sql",
            expected=expected_sql,
            actual=actual_sql,
            is_strict=True,
            is_critical_diff=True,
        ),),
        message="SQL differs",
    )


def evaluate_result_layer(
    expected_result: Any, actual_result: Any
) -> LayerReport:
    """Layer 4: Result row comparison."""
    if expected_result is None or actual_result is None:
        return LayerReport(layer="result", outcome=LayerOutcome.SKIPPED)
    if expected_result == actual_result:
        return LayerReport(layer="result", outcome=LayerOutcome.PASS)
    return LayerReport(
        layer="result",
        outcome=LayerOutcome.FAIL,
        message="Result rows differ",
    )


def evaluate_trace_layer(
    expected_stages: list[str] | None,
    actual_stages: list[str] | None,
) -> LayerReport:
    """Layer 5: Pipeline trace stage comparison."""
    if expected_stages is None or actual_stages is None:
        return LayerReport(layer="trace", outcome=LayerOutcome.SKIPPED)
    if expected_stages == actual_stages:
        return LayerReport(layer="trace", outcome=LayerOutcome.PASS)
    return LayerReport(
        layer="trace",
        outcome=LayerOutcome.FAIL,
        message=f"Trace stages differ: {expected_stages} vs {actual_stages}",
    )


def evaluate_permissions_layer(
    expected_policies: list[str] | None,
    actual_policies: list[str] | None,
    ephemeral_policy: dict[str, Any] | None = None,
) -> LayerReport:
    """Layer 6: Permissions / row-policy decisions.

    Args:
        expected_policies: List of expected row policy IDs (from golden case).
        actual_policies: List of actual row policy IDs (from system under test).
        ephemeral_policy: Field-level policy overrides for this case.
            If provided, expected_policies is replaced by deriving policies
            from ephemeral_policy structure. Format:
            {resource_name: {"fields": [...], "row_filter": "...", ...}}
    """
    if ephemeral_policy:
        # Ephemeral policy overrides: derive expected policies from structure
        expected_policies = _derive_policies_from_ephemeral(ephemeral_policy)

    if expected_policies is None or actual_policies is None:
        return LayerReport(layer="permissions", outcome=LayerOutcome.SKIPPED)
    if set(expected_policies) == set(actual_policies):
        return LayerReport(layer="permissions", outcome=LayerOutcome.PASS)
    return LayerReport(
        layer="permissions",
        outcome=LayerOutcome.FAIL,
        message=f"Row policy decisions differ: expected {expected_policies} vs actual {actual_policies}",
    )


def evaluate_nonfunctional_layer(
    latency_ms: int | None,
    token_usage: int | None,
    threshold: int = 30000,
) -> LayerReport:
    """Layer 7: Non-functional (latency, cost)."""
    if latency_ms is None:
        return LayerReport(layer="nonfunctional", outcome=LayerOutcome.SKIPPED)
    if latency_ms > threshold:
        return LayerReport(
            layer="nonfunctional",
            outcome=LayerOutcome.FAIL,
            message=f"Latency {latency_ms}ms > {threshold}ms",
        )
    return LayerReport(layer="nonfunctional", outcome=LayerOutcome.PASS)


# --- Helpers ---------------------------------------------------------------


def _derive_policies_from_ephemeral(
    ephemeral_policy: dict[str, Any],
) -> list[str]:
    """Derive expected policy IDs from ephemeral_policy structure.

    Ephemeral policies are field-level overrides that replace global policies
    for a specific case. This helper converts the structure into a list of
    policy identifiers for comparison against actual policies.

    Args:
        ephemeral_policy: Dict mapping resource names to policy specs.
                         Each spec may contain fields, row_filter, etc.

    Returns:
        List of derived policy identifiers (e.g., ["field_policy_1", ...]).
    """
    policies = []
    for resource_name, spec in ephemeral_policy.items():
        if not isinstance(spec, dict):
            continue
        # Generate a deterministic policy ID from resource + fields
        if "fields" in spec and isinstance(spec["fields"], (list, tuple)):
            fields_str = "_".join(sorted(spec["fields"]))
            policy_id = f"ephemeral_{resource_name}_{fields_str}"
            policies.append(policy_id)
        # If row_filter is present, add it to the policy ID
        if "row_filter" in spec:
            filter_str = spec["row_filter"]
            if isinstance(filter_str, str) and filter_str:
                policy_id = f"ephemeral_{resource_name}_filter_{filter_str[:16]}"
                policies.append(policy_id)
    return sorted(policies)


def _semantic_equivalent(a: Any, b: Any) -> bool:
    """Lenient comparison: set-equality for collections, equality for scalars."""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return set(map(str, a)) == set(map(str, b))
    return a == b


def case_passes(reports: tuple[LayerReport, ...]) -> bool:
    """A case passes when no layer FAILs (SKIPPED is OK)."""
    return all(r.outcome != LayerOutcome.FAIL for r in reports)
