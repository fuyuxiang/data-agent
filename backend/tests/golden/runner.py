"""Golden Set runner: executes one case, asserts the outcome, returns a report."""

from __future__ import annotations

import operator
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from tests.golden.loader import Expectation, GoldenCase, IntentExpectation, diff_in
from app.evals.layers import (
    LayerOutcome,
    LayerReport,
    Tolerance,
    evaluate_intent_layer,
    evaluate_status_layer,
    evaluate_sql_layer,
    evaluate_result_layer,
    evaluate_trace_layer,
    evaluate_permissions_layer,
    evaluate_nonfunctional_layer,
)


@dataclass(frozen=True)
class Assertion:
    """An assertion to check in a follow-up round."""

    name: str  # e.g. "result_contains_X", "latency_under_Y"
    condition: str  # e.g. "result.contains('revenue')" (for evaluation)
    expected: Any  # expected value or pattern
    operator: str  # "eq", "contains", "lt", "gt", "in", "regex", etc.


@dataclass(frozen=True)
class FollowUpRound:
    """Configuration for a follow-up round with assertions."""

    round_num: int
    prompt: str  # follow-up prompt/instruction
    assertions: tuple[Assertion, ...] = ()


@dataclass(frozen=True)
class AssertionResult:
    """Result of evaluating one assertion."""

    name: str
    passed: bool
    expected: Any
    actual: Any
    message: str = ""


@dataclass(frozen=True)
class CaseReport:
    id: str
    status: Literal["PASS", "XFAIL", "FAIL", "ERROR", "SKIPPED"]
    mode: Literal["stub", "real"]
    duration_ms: int
    message: str = ""
    diff: dict[str, tuple[Any, Any]] | None = None
    layer_reports: tuple[LayerReport, ...] = ()
    followup_assertions: tuple[AssertionResult, ...] = ()  # for followup rounds
    outcome: dict[str, Any] | None = None  # raw outcome dict for assertion evaluation




def _intent_to_dict(intent: IntentExpectation | None) -> dict[str, Any] | None:
    """Convert IntentExpectation to dict for layer evaluation."""
    if intent is None:
        return None
    return {
        "metric": intent.metric,
        "time": intent.time,
        "dimension": intent.dimension,
        "filters": intent.filters,
        "comparison": intent.comparison,
        "top_n": intent.top_n,
    }


def _get_tolerance_for_field(field: str, tolerances: dict[str, str]) -> Tolerance:
    """Get tolerance level for a specific intent field."""
    tol_str = tolerances.get(field, "lenient").lower()
    return Tolerance.STRICT if tol_str == "strict" else Tolerance.LENIENT


def _policies_from_case(case: GoldenCase) -> list[str] | None:
    """Extract policy IDs from case.policies (PolicySpec tuples)."""
    if not case.policies:
        return None
    # PolicySpec has kind (row_policy/column_deny) and field/allowed_values
    # For permissions layer, we derive a simple policy ID from the spec
    policy_ids = []
    for spec in case.policies:
        # Create a simple ID: kind + field name (or just kind if no field)
        policy_id = spec.kind
        if spec.field:
            policy_id = f"{spec.kind}[{spec.field}]"
        policy_ids.append(policy_id)
    return policy_ids if policy_ids else None


def _case_passes_from_layers(layer_reports: list[LayerReport], case: Expectation) -> bool:
    """Determine if case passes based on layer reports.

    Logic:
    - If any layer FAIL → case fails
    - If intent layer has LENIENT diffs and others PASS → XFAIL (not PASS)
    - Otherwise → case passes
    """
    for report in layer_reports:
        if report.outcome == LayerOutcome.FAIL:
            return False
    return True


def _has_lenient_intent_diff(layer_reports: list[LayerReport]) -> bool:
    """Check if intent layer has LENIENT differences."""
    for report in layer_reports:
        if report.layer == "intent" and report.outcome == LayerOutcome.PASS and report.diffs:
            # PASS with diffs means lenient mode
            return True
    return False


def _evaluate_assertion(assertion: Assertion, result: dict[str, Any]) -> AssertionResult:
    """Evaluate a single assertion against the result dict.

    Supports operators:
    - eq: equality check
    - contains: substring or item in list
    - lt, gt, le, ge: numeric comparisons
    - in: membership test
    - regex: regex pattern match
    """
    actual = None
    passed = False
    message = ""

    try:
        # Extract the actual value from result using simple dot notation
        # Examples: "result.status", "result.response_text", "result.latency_ms"
        if assertion.condition.startswith("result."):
            key = assertion.condition[7:]  # Remove "result." prefix
            actual = result.get(key)
        elif assertion.condition.startswith("result["):
            # Handle array access: result[0].field
            path_match = re.match(r"result\[(\d+)\](?:\.(\w+))?", assertion.condition)
            if path_match:
                idx = int(path_match.group(1))
                field = path_match.group(2)
                rows = result.get("rows", [])
                if idx < len(rows):
                    if field:
                        actual = rows[idx].get(field)
                    else:
                        actual = rows[idx]
        else:
            actual = result

        # Apply operator
        op_map = {
            "eq": operator.eq,
            "ne": operator.ne,
            "lt": operator.lt,
            "le": operator.le,
            "gt": operator.gt,
            "ge": operator.ge,
        }

        if assertion.operator in op_map:
            op = op_map[assertion.operator]
            passed = op(actual, assertion.expected)
        elif assertion.operator == "contains":
            # Check substring or item in container
            if isinstance(actual, str):
                passed = assertion.expected in actual
            elif isinstance(actual, (list, tuple)):
                passed = assertion.expected in actual
            else:
                passed = False
        elif assertion.operator == "in":
            # Check if actual is in expected (container)
            passed = actual in assertion.expected
        elif assertion.operator == "regex":
            # Regex match
            if isinstance(actual, str):
                passed = bool(re.search(assertion.expected, actual))
            else:
                passed = False
        else:
            message = f"unknown operator: {assertion.operator}"

    except Exception as e:
        message = f"assertion evaluation error: {e}"

    return AssertionResult(
        name=assertion.name,
        passed=passed,
        expected=assertion.expected,
        actual=actual,
        message=message,
    )


def _all_assertions_pass(assertion_results: list[AssertionResult]) -> bool:
    """Check if all assertions passed."""
    return all(r.passed for r in assertion_results)


def assert_case(outcome: dict[str, Any], expect: Expectation) -> AssertionError | None:
    """Run all assertions on outcome vs expect. Return AssertionError on first fail, else None."""
    actual_status = outcome.get("status")
    if actual_status != expect.status:
        return AssertionError(
            f"status mismatch: expected {expect.status!r}, got {actual_status!r}"
        )

    if expect.rows is not None:
        rows = outcome.get("rows") or []
        if len(rows) != expect.rows:
            return AssertionError(
                f"rows mismatch: expected {expect.rows} row(s), got {len(rows)}"
            )

    if expect.first_row is not None:
        rows = outcome.get("rows") or []
        if not rows:
            return AssertionError("first_row expected but rows is empty")
        first = rows[0]
        for key, expected_val in expect.first_row.items():
            if first.get(key) != expected_val:
                return AssertionError(
                    f"first_row[{key!r}] mismatch: expected {expected_val!r}, got {first.get(key)!r}"
                )

    if expect.citation_has:
        actual_kinds = {c.get("kind") for c in (outcome.get("citation") or [])}
        for c in expect.citation_has:
            if c.kind not in actual_kinds:
                return AssertionError(
                    f"citation_has missing kind {c.kind!r}: got {sorted(actual_kinds)}"
                )

    if expect.refused_leaks:
        text = outcome.get("response_text", "") or ""
        for token in expect.refused_leaks:
            if token in text:
                return AssertionError(
                    f"refusal leaks metadata: {token!r} found in {text!r}"
                )

    return None


def run_case(
    case: GoldenCase,
    *,
    mode: Literal["stub", "real"],
    orchestrator: Callable[..., dict[str, Any]],
    user_id_resolver: Callable[[str], int],
    ephemeral_policy: Callable[[str, tuple], None],
) -> CaseReport:
    """Execute one Golden Set case, return a CaseReport with 7-layer evaluation."""
    started = time.perf_counter()

    if case.mode_required == "real_only" and mode == "stub":
        return CaseReport(
            id=case.id, status="SKIPPED", mode=mode, duration_ms=0,
            message="mode_required=real_only",
            outcome=None,
        )
    if case.mode_required == "stub_only" and mode == "real":
        return CaseReport(
            id=case.id, status="SKIPPED", mode=mode, duration_ms=0,
            message="mode_required=stub_only",
            outcome=None,
        )

    try:
        if case.policies:
            ephemeral_policy(case.as_user, case.policies)
        user_id = user_id_resolver(case.as_user)
        outcome = orchestrator(
            question=case.question,
            user_id=user_id,
            client=None,
            llm_mode=mode,
        )
    except Exception as exc:
        return CaseReport(
            id=case.id, status="ERROR", mode=mode,
            duration_ms=int((time.perf_counter() - started) * 1000),
            message=f"{type(exc).__name__}: {exc}",
            outcome=None,
        )

    # Run basic assertion first (backward compatibility)
    err = assert_case(outcome, case.expect)
    elapsed = int((time.perf_counter() - started) * 1000)

    # Invoke all 7 layer evaluations
    layer_reports = []

    # Layer 1: Intent
    intent_tolerance = _get_tolerance_for_field("metrics", case.expect.intent_tolerances)
    layer_reports.append(
        evaluate_intent_layer(
            expected_intent=_intent_to_dict(case.expect.intent),
            actual_intent=outcome.get("intent"),
            tolerance=intent_tolerance,
        )
    )

    # Layer 2: Status
    layer_reports.append(
        evaluate_status_layer(
            expected_status=case.expect.status,
            actual_status=outcome.get("status"),
        )
    )

    # Layer 3: SQL (no expected SQL currently in golden cases)
    layer_reports.append(
        evaluate_sql_layer(
            expected_sql=None,
            actual_sql=outcome.get("sql"),
        )
    )

    # Layer 4: Result (compare row count)
    layer_reports.append(
        evaluate_result_layer(
            expected_result=case.expect.rows,
            actual_result=len(outcome.get("rows", [])) if outcome.get("rows") else None,
        )
    )

    # Layer 5: Trace (no expected trace currently in golden cases)
    layer_reports.append(
        evaluate_trace_layer(
            expected_stages=None,
            actual_stages=outcome.get("trace"),
        )
    )

    # Layer 6: Permissions (with ephemeral_policy override)
    layer_reports.append(
        evaluate_permissions_layer(
            expected_policies=_policies_from_case(case),
            actual_policies=outcome.get("policies"),
            ephemeral_policy=case.expect.ephemeral_policy,
        )
    )

    # Layer 7: Non-functional
    layer_reports.append(
        evaluate_nonfunctional_layer(
            latency_ms=outcome.get("latency_ms"),
            token_usage=outcome.get("token_usage"),
        )
    )

    # Determine final status based on layers
    # Priority: ERROR > FAIL > XFAIL > PASS

    # Check if any layer failed (but skip checking basic assertion error for now,
    # we'll integrate it with layer outcomes)
    has_layer_fail = any(r.outcome == LayerOutcome.FAIL for r in layer_reports)
    has_lenient_intent_diff = _has_lenient_intent_diff(layer_reports)

    if err is not None and has_layer_fail:
        # Both basic assertion and layer evaluation failed
        return CaseReport(
            id=case.id, status="FAIL", mode=mode,
            duration_ms=elapsed, message=str(err),
            layer_reports=tuple(layer_reports),
            outcome=outcome,
        )

    if has_layer_fail:
        # Layer evaluation found failures
        return CaseReport(
            id=case.id, status="FAIL", mode=mode,
            duration_ms=elapsed, message="Layer evaluation failed",
            layer_reports=tuple(layer_reports),
            outcome=outcome,
        )

    if has_lenient_intent_diff and err is None:
        # Intent layer has lenient diffs but other layers passed
        return CaseReport(
            id=case.id, status="XFAIL", mode=mode,
            duration_ms=elapsed,
            layer_reports=tuple(layer_reports),
            outcome=outcome,
        )

    if err is not None:
        # Basic assertion failed but layers passed (keep backward compatibility)
        return CaseReport(
            id=case.id, status="FAIL", mode=mode,
            duration_ms=elapsed, message=str(err),
            layer_reports=tuple(layer_reports),
            outcome=outcome,
        )

    # All layers and basic assertion passed
    return CaseReport(
        id=case.id, status="PASS", mode=mode, duration_ms=elapsed,
        layer_reports=tuple(layer_reports),
        outcome=outcome,
    )


def run_clarify_followup(
    case: GoldenCase,
    *,
    mode: Literal["stub", "real"],
    orchestrator: Callable[..., dict[str, Any]],
    user_id_resolver: Callable[[str], int],
    ephemeral_policy: Callable[[str, tuple], None],
    followup_assertions: list[Assertion] | None = None,
) -> tuple[CaseReport, CaseReport]:
    """Execute a followup case: first turn asks the question, expects a
    clarification; second turn submits the chosen option, expects an answer.

    Args:
        case: The Golden Case to execute
        mode: "stub" or "real"
        orchestrator: Callable that executes the query
        user_id_resolver: Callable to resolve user ID from username
        ephemeral_policy: Callable to apply temporary policies
        followup_assertions: Optional list of assertions to check on second turn result

    Returns (first_report, second_report). Both turns are executed even if
    the first one fails. Second report includes followup_assertions evaluation.
    """
    first_report = run_case(
        case,
        mode=mode,
        orchestrator=orchestrator,
        user_id_resolver=user_id_resolver,
        ephemeral_policy=ephemeral_policy,
    )

    followup_case = GoldenCase(
        id=case.id + "-followup",
        question=case.question,
        as_user=case.followup.as_user,
        expect=case.followup.expect,
        as_of=case.as_of,
        mode_required=case.mode_required,
        policies=case.policies,
        followup=None,
        notes=f"followup of {case.id}, select_option_index={case.followup.select_option_index}",
    )

    second_report = run_case(
        followup_case,
        mode=mode,
        orchestrator=orchestrator,
        user_id_resolver=user_id_resolver,
        ephemeral_policy=ephemeral_policy,
    )

    # Evaluate followup assertions if provided
    if followup_assertions and second_report.outcome:
        assertion_results = []
        for assertion in followup_assertions:
            result = _evaluate_assertion(assertion, second_report.outcome)
            assertion_results.append(result)

        # Update second_report status based on assertions
        all_pass = _all_assertions_pass(assertion_results)
        new_status = second_report.status
        new_message = second_report.message

        if not all_pass:
            # If any assertion failed, mark as FAIL
            new_status = "FAIL"
            failed = [r for r in assertion_results if not r.passed]
            new_message = f"Followup assertions failed: {len(failed)}/{len(assertion_results)}"

        second_report = CaseReport(
            id=second_report.id,
            status=new_status,
            mode=second_report.mode,
            duration_ms=second_report.duration_ms,
            message=new_message,
            diff=second_report.diff,
            layer_reports=second_report.layer_reports,
            followup_assertions=tuple(assertion_results),
            outcome=second_report.outcome,
        )

    return first_report, second_report