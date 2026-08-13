"""Eval Run archive and gating (S5).

Eval Runs are versioned, comparable, and durable. The CI gate looks at
case-level regression, not aggregate score.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from app.evals.layers import (
    LayerReport,
    case_passes,
)


class GateDecision(str, Enum):
    """Overall gate decision for an Eval Run."""

    PASS = "pass"
    FAIL_REGRESSION = "fail_regression"
    FAIL_BELOW_THRESHOLD = "fail_below_threshold"
    ERROR = "error"


@dataclass(frozen=True)
class CaseOutcome:
    """Per-case outcome aggregating all layer reports."""

    case_id: str
    case_name: str
    layer_reports: tuple[LayerReport, ...]
    case_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_name": self.case_name,
            "case_passed": self.case_passed,
            "layer_reports": [r.to_dict() for r in self.layer_reports],
        }


@dataclass(frozen=True)
class BaselineComparison:
    """Comparison of a layer report against a baseline run."""

    case_id: str
    layer: str
    baseline_outcome: str  # "pass" | "fail" | "skipped"
    current_outcome: str
    regressed: bool  # True if current is worse than baseline


@dataclass(frozen=True)
class EvalRun:
    """A versioned, durable evaluation run.

    The hash of the run is content-addressed; the same inputs produce the
    same hash, which lets us deduplicate runs and detect identical ones.
    """

    id: str
    name: str
    started_at: datetime
    completed_at: datetime | None
    semantic_revision_id: int
    prompt_version: str
    model_snapshot: str
    case_outcomes: tuple[CaseOutcome, ...] = ()
    baseline_id: str | None = None
    threshold: float = 0.95  # 95% of cases must pass
    notes: str = ""

    def hash(self) -> str:
        """Content hash for the run's configuration (not outcomes)."""
        config = {
            "name": self.name,
            "semantic_revision_id": self.semantic_revision_id,
            "prompt_version": self.prompt_version,
            "model_snapshot": self.model_snapshot,
            "threshold": self.threshold,
            "case_ids": [c.case_id for c in self.case_outcomes],
        }
        return hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def gate_decision(
        self, baseline: Optional["EvalRun"] = None
    ) -> GateDecision:
        """Decide PASS / FAIL based on regressions and threshold.

        Per S5 §2.3, regression is the first gate; threshold is second.
        """
        # Step 1: regression check
        if baseline is not None:
            comparisons = compare_to_baseline(self, baseline)
            if any(c.regressed for c in comparisons):
                return GateDecision.FAIL_REGRESSION

        # Step 2: threshold
        if not self.case_outcomes:
            return GateDecision.ERROR
        passed = sum(1 for c in self.case_outcomes if c.case_passed)
        ratio = passed / len(self.case_outcomes)
        if ratio < self.threshold:
            return GateDecision.FAIL_BELOW_THRESHOLD

        return GateDecision.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "semantic_revision_id": self.semantic_revision_id,
            "prompt_version": self.prompt_version,
            "model_snapshot": self.model_snapshot,
            "case_outcomes": [c.to_dict() for c in self.case_outcomes],
            "baseline_id": self.baseline_id,
            "threshold": self.threshold,
            "notes": self.notes,
            "hash": self.hash(),
        }


def compare_to_baseline(
    current: EvalRun, baseline: EvalRun
) -> tuple[BaselineComparison, ...]:
    """Compare each layer of each case to its baseline counterpart."""
    baseline_index: dict[tuple[str, str], LayerReport] = {
        (c.case_id, r.layer): r for c in baseline.case_outcomes for r in c.layer_reports
    }
    comparisons: list[BaselineComparison] = []
    for case in current.case_outcomes:
        for report in case.layer_reports:
            key = (case.case_id, report.layer)
            base = baseline_index.get(key)
            if base is None:
                # New layer in current; treat as regression if current is FAIL
                comparisons.append(
                    BaselineComparison(
                        case_id=case.case_id,
                        layer=report.layer,
                        baseline_outcome="skipped",
                        current_outcome=report.outcome.value,
                        regressed=report.outcome.value == "fail",
                    )
                )
                continue
            comparisons.append(
                BaselineComparison(
                    case_id=case.case_id,
                    layer=report.layer,
                    baseline_outcome=base.outcome.value,
                    current_outcome=report.outcome.value,
                    regressed=_is_regression(base.outcome.value, report.outcome.value),
                )
            )
    return tuple(comparisons)


def _is_regression(baseline: str, current: str) -> bool:
    """Regressed if current outcome is worse than baseline."""
    order = {"pass": 0, "skipped": 1, "fail": 2}
    return order.get(current, 1) > order.get(baseline, 1)


# --- Archive persistence ---------------------------------------------------


def archive_run(
    run: EvalRun, archive_dir: Path
) -> Path:
    """Write a run to disk as a single JSON file. Returns the file path."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{run.id}.json"
    target.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
    return target


def load_run(archive_path: Path) -> EvalRun:
    """Load a previously archived Eval Run from disk."""
    data = json.loads(archive_path.read_text())
    case_outcomes = tuple(
        CaseOutcome(
            case_id=c["case_id"],
            case_name=c["case_name"],
            layer_reports=tuple(
                LayerReport(
                    layer=r["layer"],
                    outcome=r["outcome"],
                    message=r.get("message", ""),
                )
                for r in c["layer_reports"]
            ),
            case_passed=c["case_passed"],
        )
        for c in data["case_outcomes"]
    )
    return EvalRun(
        id=data["id"],
        name=data["name"],
        started_at=datetime.fromisoformat(data["started_at"]),
        completed_at=(
            datetime.fromisoformat(data["completed_at"])
            if data["completed_at"]
            else None
        ),
        semantic_revision_id=data["semantic_revision_id"],
        prompt_version=data["prompt_version"],
        model_snapshot=data["model_snapshot"],
        case_outcomes=case_outcomes,
        baseline_id=data.get("baseline_id"),
        threshold=data["threshold"],
        notes=data.get("notes", ""),
    )
