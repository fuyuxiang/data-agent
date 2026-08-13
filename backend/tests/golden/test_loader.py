"""Tests for the Golden Set YAML loader and diff_in."""

from datetime import date
from pathlib import Path

import pytest

from app.intent.schema import ComparisonKind, TimeGrain, TimeRange
from tests.golden.loader import (
    Expectation,
    IntentExpectation,
    PolicySpec,
    diff_in,
    load_cases,
)


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases"
    d.mkdir()
    return d


def _yaml(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def test_load_single_minimal_case(cases_dir: Path):
    _yaml(
        cases_dir,
        "g001.yaml",
        """
id: G-001
question: 总销售额
as_user: admin
expect:
  status: ANSWERED
""",
    )
    cases = load_cases(cases_dir)
    assert len(cases) == 1
    case = cases[0]
    assert case.id == "G-001"
    assert case.question == "总销售额"
    assert case.as_user == "admin"
    assert case.expect.status == "ANSWERED"
    assert case.expect.intent is None
    assert case.expect.rows is None


def test_load_case_with_full_intent_expectation(cases_dir: Path):
    _yaml(
        cases_dir,
        "g002.yaml",
        """
id: G-002
question: 上月销售额
as_user: admin
expect:
  status: ANSWERED
  intent:
    metric: sales_revenue
    time:
      start: 2026-08-01
      end: 2026-08-31
      grain: MONTH
      expression: 本月
  rows: 5
  first_row:
    province: 江苏
    sales_revenue: 142000
  citation_has:
    - kind: metric
      text: sales_revenue
    - kind: permission
""",
    )
    case = load_cases(cases_dir)[0]
    assert case.expect.intent.metric == "sales_revenue"
    assert case.expect.intent.time.start == date(2026, 8, 1)
    assert case.expect.intent.time.grain == TimeGrain.MONTH
    assert case.expect.rows == 5
    assert case.expect.first_row == {"province": "江苏", "sales_revenue": 142000}
    assert len(case.expect.citation_has) == 2


def test_load_case_with_policy_and_refused_leaks(cases_dir: Path):
    _yaml(
        cases_dir,
        "g050.yaml",
        """
id: G-050
as_user: analyst_east
policies:
  - kind: row_policy
    field: region_code
    allowed_values: [EC]
question: 上月广东销售额
expect:
  status: REFUSED
  refused_leaks: [sample.orders, 广东, region_code]
""",
    )
    case = load_cases(cases_dir)[0]
    assert case.policies == (
        PolicySpec(kind="row_policy", field="region_code", allowed_values=("EC",)),
    )
    assert case.expect.refused_leaks == ("sample.orders", "广东", "region_code")


def test_load_case_with_followup(cases_dir: Path):
    _yaml(
        cases_dir,
        "g043.yaml",
        """
id: G-043
question: 财务确认收入是多少
expect_first:
  status: CLARIFYING
  clarify_kind: METRIC
followup:
  as_user: admin
  select_option_index: 0
  expect:
    status: ANSWERED
""",
    )
    case = load_cases(cases_dir)[0]
    assert case.followup is not None
    assert case.followup.select_option_index == 0
    assert case.followup.expect.status == "ANSWERED"


def test_load_case_with_default_as_of(cases_dir: Path):
    _yaml(
        cases_dir,
        "g099.yaml",
        """
id: G-099
question: 销售额
as_user: admin
expect:
  status: ANSWERED
""",
    )
    case = load_cases(cases_dir)[0]
    assert case.as_of == date(2026, 8, 12)


def test_load_case_with_explicit_as_of(cases_dir: Path):
    _yaml(
        cases_dir,
        "g098.yaml",
        """
as_of: 2026-08-11
id: G-098
question: 销售额
as_user: admin
expect:
  status: ANSWERED
""",
    )
    case = load_cases(cases_dir)[0]
    assert case.as_of == date(2026, 8, 11)


def test_invalid_status_raises_at_collection(cases_dir: Path):
    _yaml(
        cases_dir,
        "g_bad.yaml",
        """
id: G-BAD
question: 销售额
as_user: admin
expect:
  status: NOT_A_REAL_STATUS
""",
    )
    with pytest.raises(Exception):
        load_cases(cases_dir)


def test_missing_id_raises_at_collection(cases_dir: Path):
    _yaml(
        cases_dir,
        "g_noid.yaml",
        """
question: 销售额
as_user: admin
expect:
  status: ANSWERED
""",
    )
    with pytest.raises(Exception):
        load_cases(cases_dir)


def test_load_cases_recurses_into_subdirectories(cases_dir: Path):
    sub = cases_dir / "simple"
    sub.mkdir()
    _yaml(
        sub,
        "g001.yaml",
        "id: G-001\nquestion: q1\nas_user: admin\nexpect:\n  status: ANSWERED\n",
    )
    _yaml(
        sub,
        "g002.yaml",
        "id: G-002\nquestion: q2\nas_user: admin\nexpect:\n  status: ANSWERED\n",
    )
    cases = load_cases(cases_dir)
    assert {c.id for c in cases} == {"G-001", "G-002"}


def test_duplicate_ids_raise_at_collection(cases_dir: Path):
    _yaml(
        cases_dir,
        "g001.yaml",
        "id: G-001\nquestion: q1\nas_user: admin\nexpect:\n  status: ANSWERED\n",
    )
    _yaml(
        cases_dir,
        "g001_dup.yaml",
        "id: G-001\nquestion: q1\nas_user: admin\nexpect:\n  status: ANSWERED\n",
    )
    with pytest.raises(Exception, match="duplicate"):
        load_cases(cases_dir)


def test_diff_in_returns_empty_when_all_match():
    """diff_in returns empty dict iff every slot in expected matches actual."""
    intent = type(
        "I",
        (),
        {
            "metric": "sales_revenue",
            "time": TimeRange(
                start=date(2026, 8, 1),
                end=date(2026, 8, 31),
                grain=TimeGrain.MONTH,
                expression="本月",
            ),
            "dimension": ("province",),
            "filters": (),
            "comparison": ComparisonKind.MOM,
            "top_n": 5,
        },
    )()
    expected = IntentExpectation(
        metric="sales_revenue",
        time=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH),
        dimension=("province",),
        comparison=ComparisonKind.MOM,
        top_n=5,
    )
    assert diff_in(intent, expected) == {}


def test_diff_in_reports_each_differing_slot():
    intent = type(
        "I",
        (),
        {
            "metric": "gross_margin",
            "time": TimeRange(
                start=date(2026, 8, 1),
                end=date(2026, 8, 31),
                grain=TimeGrain.MONTH,
            ),
            "dimension": (),
            "filters": (),
            "comparison": None,
            "top_n": 3,
        },
    )()
    expected = IntentExpectation(
        metric="sales_revenue",
        time=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31), grain=TimeGrain.MONTH),
        top_n=5,
    )
    diff = diff_in(intent, expected)
    assert set(diff.keys()) == {"metric", "top_n"}
    assert diff["metric"] == ("sales_revenue", "gross_margin")
    assert diff["top_n"] == (5, 3)