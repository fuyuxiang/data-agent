from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_JOB = "waiting_job"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    ACCEPTED = "ACCEPTED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    session_id: str
    actor_id: str
    workspace_id: str
    contract_version: int
    policy_version: str
    source_scope: tuple[str, ...]
    allowed_tool_ids: tuple[str, ...]
    parent_run_id: str | None
    budget_id: str
    lease_epoch: int


@dataclass(frozen=True)
class TaskContract:
    """The four user-visible requirement sections plus enforceable constraints."""

    objective: str
    coverage: str
    dimensions: tuple[str, ...]
    deliverables: tuple[str, ...]
    source_scope: tuple[str, ...]
    time_range: dict[str, Any] = field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    metrics: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    confirmed_assumptions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TaskContract":
        if not isinstance(payload, dict):
            raise ValueError("任务契约必须是对象")
        objective = str(payload.get("objective") or payload.get("business_goal") or "").strip()
        coverage = str(payload.get("coverage") or payload.get("statistical_scope") or "").strip()
        dimensions = _strings(payload.get("dimensions") or payload.get("view_dimensions"))
        deliverables = _strings(payload.get("deliverables") or payload.get("delivery_formats"))
        source_scope = _strings(payload.get("source_scope") or payload.get("source_ids"))
        if not objective:
            raise ValueError("业务分析目标不能为空")
        if not coverage:
            raise ValueError("统计覆盖范围不能为空")
        if not dimensions:
            raise ValueError("数据查看维度不能为空")
        if not deliverables:
            raise ValueError("成果交付形式不能为空")
        if len(source_scope) > 100:
            raise ValueError("单次任务最多选择 100 个来源")
        return cls(
            objective=objective[:4000], coverage=coverage[:4000],
            dimensions=dimensions, deliverables=deliverables, source_scope=source_scope,
            time_range=dict(payload.get("time_range") or {}),
            timezone=str(payload.get("timezone") or "Asia/Shanghai")[:80],
            metrics=_strings(payload.get("metrics")),
            budget=dict(payload.get("budget") or {}),
            confirmed_assumptions=_strings(payload.get("confirmed_assumptions") or payload.get("assumptions")),
            unresolved=_strings(payload.get("unresolved")),
            acceptance=_strings(payload.get("acceptance")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ).hexdigest()


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    protocol: str
    model: str
    content: str
    tool_calls: tuple[ModelToolCall, ...]
    finish_reason: str
    refusal: str | None
    usage: dict[str, int]


@dataclass(frozen=True)
class ToolSpec:
    id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)
    mutability: str = "read"
    idempotency: str = "idempotent"
    timeout_seconds: int = 60
    cancellable: bool = False
    cost_kind: str = "tool_calls"
    scopes: tuple[str, ...] = ("analysis",)

    def as_model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.id,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    logical_action_id: str
    attempt_id: str
    job_id: str | None = None
    output_refs: tuple[str, ...] = ()
    schema_refs: tuple[str, ...] = ()
    preview: Any = None
    completeness: str = "unknown"
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    provenance_ref: str | None = None
    validation_status: str = "not_evaluated"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError("字段必须是字符串或字符串数组")
    result = tuple(dict.fromkeys(str(item).strip()[:1000] for item in values if str(item).strip()))
    if len(result) > 200:
        raise ValueError("字段项目过多")
    return result
