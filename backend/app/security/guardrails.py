"""Cost guardrails (spec M-13).

Uses PostgreSQL's EXPLAIN (FORMAT JSON) without ANALYZE: the planner's row and
cost estimates are read without executing anything. Any failure to obtain a
plan is treated as REJECT — assuming an unreadable query is cheap is how a
guardrail becomes decorative.
"""

import json
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings


class CostVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    verdict: CostVerdict
    estimated_rows: int
    estimated_cost: float
    message: str = ""


class QueryTooExpensiveError(Exception):
    def __init__(self, estimate: CostEstimate) -> None:
        self.estimate = estimate
        super().__init__(estimate.message)


def estimate_cost(connection: Connection, sql: str, settings: Settings) -> CostEstimate:
    try:
        raw = connection.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar_one()
    except SQLAlchemyError as error:
        return CostEstimate(
            verdict=CostVerdict.REJECT,
            estimated_rows=0,
            estimated_cost=0.0,
            message=f"无法获取查询计划，按拒绝处理：{error.__class__.__name__}",
        )

    plan = json.loads(raw) if isinstance(raw, str) else raw
    try:
        root = plan[0]["Plan"]
        rows = int(root["Plan Rows"])
        cost = float(root["Total Cost"])
    except (KeyError, IndexError, TypeError, ValueError):
        return CostEstimate(
            verdict=CostVerdict.REJECT,
            estimated_rows=0,
            estimated_cost=0.0,
            message="查询计划结构无法解析，按拒绝处理",
        )

    if rows >= settings.cost_reject_rows:
        return CostEstimate(
            CostVerdict.REJECT,
            rows,
            cost,
            f"预估扫描 {rows} 行，超过上限 {settings.cost_reject_rows} 行，已拒绝执行",
        )
    if rows >= settings.cost_warn_rows:
        return CostEstimate(
            CostVerdict.WARN,
            rows,
            cost,
            f"预估扫描 {rows} 行，数据量较大，建议缩小时间范围",
        )
    return CostEstimate(CostVerdict.PASS, rows, cost)


def assert_affordable(connection: Connection, sql: str, settings: Settings) -> CostEstimate:
    estimate = estimate_cost(connection, sql, settings)
    if estimate.verdict == CostVerdict.REJECT:
        raise QueryTooExpensiveError(estimate)
    return estimate