from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ...core.database import Database, utcnow


@dataclass(frozen=True)
class ValidationOutcome:
    status: str
    reason: str
    details: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    id: str
    version: str
    layer: str
    severity: str
    weight: float
    check: Callable[[dict[str, Any]], ValidationOutcome]


class ValidationEngine:
    VALID_STATUSES = frozenset({"PASS", "WARN", "FAIL", "UNKNOWN", "NOT_APPLICABLE"})

    def __init__(self, database: Database, rules: list[Rule], *, scoring_version: str = "quality-v1"):
        self.db = database
        self.rules = list(rules)
        self.scoring_version = scoring_version

    def evaluate(self, *, run_id: str, workspace_id: str, subject_ref: str, context: dict[str, Any]) -> dict[str, Any]:
        outcomes = []
        for rule in self.rules:
            try:
                outcome = rule.check(context)
            except Exception as exc:
                outcome = ValidationOutcome("UNKNOWN", "规则执行失败", {"error": str(exc)})
            if outcome.status not in self.VALID_STATUSES:
                raise ValueError(f"验证规则 {rule.id} 返回了无效状态")
            record_id = self.db.new_id("validation")
            payload = {
                "reason": outcome.reason, "details": outcome.details,
                "layer": rule.layer, "weight": rule.weight, "scoring_version": self.scoring_version,
            }
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO validation_results(
                           id,workspace_id,run_id,rule_id,rule_version,subject_ref,status,severity,payload,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id, workspace_id, run_id, rule.id, rule.version, subject_ref,
                        outcome.status, rule.severity, json.dumps(payload, ensure_ascii=False, default=str), utcnow(),
                    ),
                )
            outcomes.append({
                "id": record_id, "rule_id": rule.id, "rule_version": rule.version,
                "subject_ref": subject_ref, "status": outcome.status, "severity": rule.severity,
                **payload,
            })
        return self.summarize(outcomes)

    @staticmethod
    def summarize(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        applicable = [item for item in outcomes if item["status"] != "NOT_APPLICABLE"]
        known = [item for item in applicable if item["status"] != "UNKNOWN"]
        covered_weight = sum(float(item["weight"]) for item in known)
        expected_weight = sum(float(item["weight"]) for item in applicable)
        earned = sum(
            float(item["weight"]) * {"PASS": 1.0, "WARN": 0.5, "FAIL": 0.0}.get(item["status"], 0.0)
            for item in known
        )
        blocking = [
            item for item in applicable
            if item["severity"] == "blocking" and item["status"] in {"FAIL", "UNKNOWN"}
        ]
        return {
            "status": "FAIL" if any(item["status"] == "FAIL" for item in blocking) else "UNKNOWN" if blocking else "PASS",
            "quality_score": round(earned / covered_weight * 100, 2) if covered_weight else None,
            "coverage": round(covered_weight / expected_weight * 100, 2) if expected_weight else None,
            "scoring_note": "规则质量分，不是答案正确概率",
            "blocking_issues": blocking,
            "issues": [item for item in applicable if item["status"] in {"WARN", "FAIL", "UNKNOWN"}],
            "items": outcomes,
        }

    def list_for_run(self, run_id: str, *, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM validation_results WHERE run_id=? AND workspace_id=? ORDER BY created_at,id",
                (run_id, workspace_id),
            ).fetchall()
        return [dict(row) | json.loads(row["payload"]) for row in rows]


def outcome(status: str, reason: str, **details: Any) -> ValidationOutcome:
    return ValidationOutcome(status, reason, details)
