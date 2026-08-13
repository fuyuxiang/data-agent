"""Golden Set YAML loader and diff_in helper.

A single YAML file is one question. The loader returns a list of GoldenCase
objects. Strong schema validation happens at collection time so a malformed
case stops the run immediately rather than producing a cryptic failure mid-suite.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.intent.schema import ComparisonKind, FilterOperator, TimeGrain
from app.evals.layers import Tolerance

_VALID_STATUSES = ("ANSWERED", "CLARIFYING", "REFUSED", "FAILED")
_VALID_POLICY_KINDS = ("row_policy", "column_deny")
_VALID_TOP_N = 5
_DEFAULT_AS_OF = date(2026, 8, 12)


@dataclass(frozen=True)
class PolicySpec:
    kind: str
    field: str = ""
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentExpectation:
    metric: str = ""
    time: Any = None  # TimeRange
    dimension: tuple[str, ...] = ()
    filters: tuple[dict, ...] = ()
    comparison: ComparisonKind | None = None
    top_n: int | None = None


@dataclass(frozen=True)
class CitationExpectation:
    kind: str
    text: str = ""


@dataclass(frozen=True)
class Expectation:
    status: str
    intent: IntentExpectation | None = None
    rows: int | None = None
    first_row: dict | None = None
    citation_has: tuple[CitationExpectation, ...] = ()
    refused_leaks: tuple[str, ...] = ()
    # Field-level tolerance for intent differences (replaces old "xfail" string)
    # Default: all fields LENIENT (backward compatible with xfail semantics)
    intent_tolerances: dict[str, str] = field(default_factory=lambda: {
        "metrics": "lenient",
        "time": "lenient",
        "dimensions": "lenient",
        "filters": "lenient",
        "comparison": "lenient",
        "top_n": "lenient",
    })
    clarify_kind: str | None = None
    # Field-level permission policies that override global policies for this case.
    # Format: {resource_name: {"fields": [...], "row_filter": "...", ...}}
    # Applied by permission_layer evaluation instead of global policy.
    ephemeral_policy: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class FollowupSpec:
    as_user: str
    select_option_index: int
    expect: Expectation


@dataclass(frozen=True)
class GoldenCase:
    id: str
    question: str
    as_user: str
    expect: Expectation
    as_of: date = _DEFAULT_AS_OF
    mode_required: str = "any"
    policies: tuple[PolicySpec, ...] = ()
    followup: FollowupSpec | None = None
    notes: str = ""


def _load_time(payload: dict) -> Any:
    from app.intent.schema import TimeRange

    grain_raw = payload.get("grain", "month")
    grain = TimeGrain(grain_raw.lower() if isinstance(grain_raw, str) else grain_raw)
    return TimeRange(
        start=_coerce_date(payload["start"]),
        end=_coerce_date(payload["end"]),
        grain=grain,
        expression=payload.get("expression", ""),
    )


def _load_intent(payload: dict | None) -> IntentExpectation | None:
    if payload is None:
        return None
    return IntentExpectation(
        metric=payload.get("metric", ""),
        time=_load_time(payload["time"]) if "time" in payload else None,
        dimension=tuple(payload.get("dimension", ())),
        filters=tuple(payload.get("filters", ())),
        comparison=ComparisonKind(payload["comparison"]) if "comparison" in payload else None,
        top_n=payload.get("top_n"),
    )


def _load_expect(payload: dict) -> Expectation:
    status = payload["status"]
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown status: {status}")

    citation_has = tuple(
        CitationExpectation(kind=item["kind"], text=item.get("text", ""))
        for item in payload.get("citation_has", ())
    )

    # Load intent tolerances from YAML, or use defaults
    intent_tolerances = payload.get("intent_tolerances", {})
    if not isinstance(intent_tolerances, dict):
        raise ValueError(f"intent_tolerances must be a dict, got {type(intent_tolerances)}")

    # Validate tolerance values
    valid_tolerances = {"strict", "lenient"}
    for field, tol in intent_tolerances.items():
        if tol not in valid_tolerances:
            raise ValueError(f"intent_tolerances[{field!r}] must be 'strict' or 'lenient', got {tol!r}")

    return Expectation(
        status=status,
        intent=_load_intent(payload.get("intent")),
        rows=payload.get("rows"),
        first_row=payload.get("first_row"),
        citation_has=citation_has,
        refused_leaks=tuple(payload.get("refused_leaks", ())),
        intent_tolerances=intent_tolerances,
        clarify_kind=payload.get("clarify_kind"),
    )


def _load_policy(payload: dict) -> PolicySpec:
    kind = payload["kind"]
    if kind not in _VALID_POLICY_KINDS:
        raise ValueError(f"unknown policy kind: {kind}")
    return PolicySpec(
        kind=kind,
        field=payload.get("field", ""),
        allowed_values=tuple(payload.get("allowed_values", ())),
    )


def _load_followup(payload: dict, root: dict | None = None) -> FollowupSpec:
    root = root or {}
    return FollowupSpec(
        as_user=payload.get("as_user", root.get("as_user", "admin")),
        select_option_index=payload["select_option_index"],
        expect=_load_expect(payload.get("expect") or root["expect_second"]),
    )


def _coerce_date(value: Any) -> date:
    """YAML auto-parses ISO dates; accept either a date or an ISO string."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _load_yaml(path: Path) -> GoldenCase:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    if "id" not in raw:
        raise ValueError(f"{path}: missing id")
    has_followup = "followup" in raw
    return GoldenCase(
        id=raw["id"],
        question=raw["question"],
        as_user=raw.get("as_user", "admin"),
        as_of=_coerce_date(raw["as_of"]) if "as_of" in raw else _DEFAULT_AS_OF,
        mode_required=raw.get("mode_required", "any"),
        expect=(
            _load_expect(raw["expect"])
            if "expect" in raw
            else _load_expect(raw["expect_first"])
        ),
        policies=tuple(_load_policy(item) for item in raw.get("policies", ())),
        followup=_load_followup(raw["followup"], raw) if has_followup else None,
        notes=raw.get("notes", ""),
    )


def load_cases(cases_dir: Path) -> list[GoldenCase]:
    """Recursively load every YAML case under ``cases_dir``.

    Raises on schema errors or duplicate ids so a bad case is caught at
    collection rather than mid-suite.
    """
    seen: set[str] = set()
    cases: list[GoldenCase] = []
    for path in sorted(cases_dir.rglob("*.yaml")):
        case = _load_yaml(path)
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


def diff_in(actual: Any, expected: IntentExpectation) -> dict[str, tuple[Any, Any]]:
    """Return non-empty mapping iff at least one slot differs.

    Keys are the slot names from ``IntentExpectation``; values are
    ``(expected, actual)``. Empty dict means full match. Time is compared on
    the structural fields that matter for a query (start/end/grain) — the
    ``expression`` field is the user-facing paraphrase and is intentionally
    ignored so test fixtures don't have to track it.
    """
    diff: dict[str, tuple[Any, Any]] = {}

    if expected.metric and getattr(actual, "metric", None) != expected.metric:
        diff["metric"] = (expected.metric, getattr(actual, "metric", None))

    if expected.time is not None:
        actual_time = getattr(actual, "time", None)
        if actual_time is None or actual_time.start != expected.time.start or actual_time.end != expected.time.end or actual_time.grain != expected.time.grain:
            diff["time"] = (expected.time, actual_time)

    expected_dimension = tuple(expected.dimension or ())
    actual_dimension = tuple(getattr(actual, "dimension", ()) or ())
    if expected_dimension and actual_dimension != expected_dimension:
        diff["dimension"] = (expected_dimension, actual_dimension)

    expected_filters = tuple(expected.filters or ())
    actual_filters = tuple(getattr(actual, "filters", ()) or ())
    if expected_filters and actual_filters != expected_filters:
        diff["filters"] = (expected_filters, actual_filters)

    if (
        expected.comparison is not None
        and getattr(actual, "comparison", None) != expected.comparison
    ):
        diff["comparison"] = (expected.comparison, getattr(actual, "comparison", None))

    if expected.top_n is not None and getattr(actual, "top_n", None) != expected.top_n:
        diff["top_n"] = (expected.top_n, getattr(actual, "top_n", None))

    return diff