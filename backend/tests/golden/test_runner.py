"""Tests for the Golden Set runner and assert_case helper."""

from datetime import date
from typing import Any

import pytest

from app.intent.schema import ComparisonKind, TimeGrain, TimeRange
from tests.golden.loader import (
    CitationExpectation,
    Expectation,
    FollowupSpec,
    GoldenCase,
    IntentExpectation,
)
from tests.golden.runner import (
    CaseReport,
    assert_case,
    run_case,
    run_clarify_followup,
)


def _outcome(
    *,
    status: str = "ANSWERED",
    rows: list[dict] | None = None,
    citation: list[dict] | None = None,
    response_text: str = "",
    intent: Any = None,
) -> dict:
    return {
        "status": status,
        "rows": rows or [],
        "citation": citation or [],
        "response_text": response_text,
        "intent": intent,
    }


def test_assert_case_passes_when_status_and_rows_match():
    expect = Expectation(status="ANSWERED", rows=5)
    outcome = _outcome(status="ANSWERED", rows=[{"x": 1}] * 5)
    assert assert_case(outcome, expect) is None


def test_assert_case_fails_on_status_mismatch():
    expect = Expectation(status="ANSWERED")
    outcome = _outcome(status="REFUSED")
    err = assert_case(outcome, expect)
    assert isinstance(err, AssertionError)
    assert "status" in str(err)


def test_assert_case_fails_on_rows_mismatch():
    expect = Expectation(status="ANSWERED", rows=5)
    outcome = _outcome(status="ANSWERED", rows=[{"x": 1}] * 3)
    err = assert_case(outcome, expect)
    assert isinstance(err, AssertionError)
    assert "rows" in str(err)


def test_assert_case_fails_on_first_row_mismatch():
    expect = Expectation(status="ANSWERED", first_row={"province": "江苏"})
    outcome = _outcome(status="ANSWERED", rows=[{"province": "浙江"}])
    err = assert_case(outcome, expect)
    assert isinstance(err, AssertionError)
    assert "first_row" in str(err)


def test_assert_case_passes_when_first_row_subset_matches():
    """first_row with subset of keys is OK (extra columns OK)."""
    expect = Expectation(status="ANSWERED", first_row={"province": "江苏"})
    outcome = _outcome(
        status="ANSWERED",
        rows=[{"province": "江苏", "sales_revenue": 100}],
    )
    assert assert_case(outcome, expect) is None


def test_assert_case_passes_when_citation_has_match():
    expect = Expectation(
        status="ANSWERED",
        citation_has=(
            CitationExpectation(kind="metric"),
            CitationExpectation(kind="permission"),
        ),
    )
    outcome = _outcome(
        status="ANSWERED",
        citation=[
            {"kind": "metric", "text": "sales_revenue"},
            {"kind": "permission", "text": "EC only"},
            {"kind": "time", "text": "2026-08"},
        ],
    )
    assert assert_case(outcome, expect) is None


def test_assert_case_fails_when_citation_kind_missing():
    expect = Expectation(
        status="ANSWERED",
        citation_has=(CitationExpectation(kind="metric"),),
    )
    outcome = _outcome(
        status="ANSWERED",
        citation=[{"kind": "time", "text": "2026-08"}],
    )
    err = assert_case(outcome, expect)
    assert isinstance(err, AssertionError)
    assert "citation_has" in str(err)


def test_assert_case_fails_when_refused_leak_present():
    expect = Expectation(
        status="REFUSED",
        refused_leaks=("sample.orders",),
    )
    outcome = _outcome(
        status="REFUSED",
        response_text="查询 sample.orders 时被拒绝",
    )
    err = assert_case(outcome, expect)
    assert isinstance(err, AssertionError)
    assert "leaks" in str(err)


def test_assert_case_passes_when_no_leak_in_refusal():
    expect = Expectation(
        status="REFUSED",
        refused_leaks=("sample.orders", "广东"),
    )
    outcome = _outcome(
        status="REFUSED",
        response_text="你没有访问该数据的权限。",
    )
    assert assert_case(outcome, expect) is None


def test_case_report_is_frozen():
    report = CaseReport(
        id="G-001",
        status="PASS",
        mode="stub",
        duration_ms=42,
    )
    with pytest.raises(Exception):
        report.id = "G-002"  # type: ignore[misc]


def test_run_clarify_followup_returns_two_reports():
    case = GoldenCase(
        id="G-043",
        question="财务确认收入是多少",
        as_user="admin",
        expect=Expectation(status="CLARIFYING", clarify_kind="METRIC"),
        followup=FollowupSpec(
            as_user="admin",
            select_option_index=0,
            expect=Expectation(status="ANSWERED"),
        ),
    )

    call_count = {"n": 0}

    def fake_orchestrator(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "status": "CLARIFYING",
                "rows": [],
                "citation": [],
                "response_text": "",
                "intent": None,
                "clarify_kind": "METRIC",
                "options": [{"label": "含税订单金额", "value": "sales_revenue"}],
            }
        return {
            "status": "ANSWERED",
            "rows": [{"province": "江苏", "sales_revenue": 142000}],
            "citation": [{"kind": "metric", "text": "sales_revenue"}],
            "response_text": "",
            "intent": None,
        }

    first, second = run_clarify_followup(
        case,
        mode="stub",
        orchestrator=fake_orchestrator,
        user_id_resolver=lambda _: 1,
        ephemeral_policy=lambda *_: None,
    )
    assert first.status == "PASS"
    assert second.status == "PASS"


def test_run_clarify_followup_fails_first_turn_returns_propagates():
    case = GoldenCase(
        id="G-043",
        question="财务确认收入是多少",
        as_user="admin",
        expect=Expectation(status="ANSWERED"),  # wrong, should be CLARIFYING
        followup=FollowupSpec(
            as_user="admin",
            select_option_index=0,
            expect=Expectation(status="ANSWERED"),
        ),
    )

    call_count = {"n": 0}

    def fake_orchestrator(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "status": "CLARIFYING",
                "rows": [],
                "citation": [],
                "response_text": "",
                "intent": None,
                "clarify_kind": "METRIC",
                "options": [{"label": "含税订单金额", "value": "sales_revenue"}],
            }
        return {
            "status": "ANSWERED",
            "rows": [{"province": "江苏", "sales_revenue": 142000}],
            "citation": [{"kind": "metric", "text": "sales_revenue"}],
            "response_text": "",
            "intent": None,
        }

    first, second = run_clarify_followup(
        case,
        mode="stub",
        orchestrator=fake_orchestrator,
        user_id_resolver=lambda _: 1,
        ephemeral_policy=lambda *_: None,
    )
    assert first.status == "FAIL"
    # Second turn is still executed
    assert second.status == "PASS"