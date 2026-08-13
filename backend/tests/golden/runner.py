"""Golden Set runner: executes one case, asserts the outcome, returns a report."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from tests.golden.loader import Expectation, GoldenCase, diff_in


@dataclass(frozen=True)
class CaseReport:
    id: str
    status: Literal["PASS", "XFAIL", "FAIL", "ERROR", "SKIPPED"]
    mode: Literal["stub", "real"]
    duration_ms: int
    message: str = ""
    diff: dict[str, tuple[Any, Any]] | None = None


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
    """Execute one Golden Set case, return a CaseReport."""
    started = time.perf_counter()

    if case.mode_required == "real_only" and mode == "stub":
        return CaseReport(
            id=case.id, status="SKIPPED", mode=mode, duration_ms=0,
            message="mode_required=real_only",
        )
    if case.mode_required == "stub_only" and mode == "real":
        return CaseReport(
            id=case.id, status="SKIPPED", mode=mode, duration_ms=0,
            message="mode_required=stub_only",
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
        )

    # Intent diff in real mode.
    if mode == "real" and case.expect.intent is not None:
        intent_diff = diff_in(outcome.get("intent"), case.expect.intent)
        if intent_diff:
            if case.expect.intent_diff == "xfail":
                return CaseReport(
                    id=case.id, status="XFAIL", mode=mode,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    diff=intent_diff,
                )
            return CaseReport(
                id=case.id, status="FAIL", mode=mode,
                duration_ms=int((time.perf_counter() - started) * 1000),
                diff=intent_diff,
                message="real-mode intent mismatch and intent_diff=fail",
            )

    err = assert_case(outcome, case.expect)
    elapsed = int((time.perf_counter() - started) * 1000)
    if err is not None:
        return CaseReport(
            id=case.id, status="FAIL", mode=mode,
            duration_ms=elapsed, message=str(err),
        )
    return CaseReport(id=case.id, status="PASS", mode=mode, duration_ms=elapsed)


def run_clarify_followup(
    case: GoldenCase,
    *,
    mode: Literal["stub", "real"],
    orchestrator: Callable[..., dict[str, Any]],
    user_id_resolver: Callable[[str], int],
    ephemeral_policy: Callable[[str, tuple], None],
) -> tuple[CaseReport, CaseReport]:
    """Execute a followup case: first turn asks the question, expects a
    clarification; second turn submits the chosen option, expects an answer.

    Returns (first_report, second_report). Both turns are executed even if
    the first one fails.
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

    return first_report, second_report