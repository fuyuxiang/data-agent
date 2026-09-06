from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .contracts import RunContext, ToolResult, ToolSpec, ToolStatus
from .store import RunStore


ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


@dataclass(frozen=True)
class ExecutedTool:
    result: ToolResult
    value: dict[str, Any]
    events: tuple[tuple[str, dict[str, Any]], ...] = ()


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.id in self._tools:
            raise ValueError(f"工具重复注册：{spec.id}")
        self._tools[spec.id] = RegisteredTool(spec, handler)

    def get(self, tool_id: str) -> RegisteredTool:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise ValueError(f"未知工具：{tool_id}") from exc

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        return [value.spec for key, value in self._tools.items() if allowed is None or key in allowed]

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._tools)


class ToolExecutor:
    """The single permission/budget/action boundary for every agent tool."""

    def __init__(self, store: RunStore, registry: ToolRegistry):
        self.store = store
        self.registry = registry

    def effective_tools(
        self,
        context: RunContext,
        *,
        skill_tools: set[str] | None = None,
        child_tools: set[str] | None = None,
    ) -> set[str]:
        effective = set(context.allowed_tool_ids) & set(self.registry.ids)
        if skill_tools:
            effective &= skill_tools
        if child_tools:
            effective &= child_tools
        return effective

    def schemas(self, context: RunContext, **constraints: Any) -> list[dict[str, Any]]:
        effective = self.effective_tools(context, **constraints)
        return [spec.as_model_tool() for spec in self.registry.specs(effective)]

    def execute(
        self,
        *,
        context: RunContext,
        decision_id: str,
        call_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        skill_tools: set[str] | None = None,
        child_tools: set[str] | None = None,
    ) -> ExecutedTool:
        if tool_id not in self.effective_tools(context, skill_tools=skill_tools, child_tools=child_tools):
            raise PermissionError(f"有效策略未授权工具：{tool_id}")
        registered = self.registry.get(tool_id)
        _validate(arguments, registered.spec.input_schema)
        logical_action_id = f"{decision_id}:{call_id}"
        action, attempt = self.store.begin_action(
            context.run_id, decision_id, logical_action_id, tool_id, arguments,
            lease_epoch=context.lease_epoch, reserve_kind=registered.spec.cost_kind,
            reserve_unit="seconds" if registered.spec.cost_kind == "remote_compute_seconds" else "calls",
        )
        try:
            raw = registered.handler(arguments)
            if isinstance(raw, tuple) and len(raw) == 2:
                value, raw_events = raw
                events = tuple(raw_events or ())
            else:
                value, events = raw, ()
            value = value if isinstance(value, dict) else {"value": value}
            raw_status = str(value.get("status") or "").upper()
            if raw_status == ToolStatus.ACCEPTED.value:
                tool_status, action_status = ToolStatus.ACCEPTED, "accepted"
            elif raw_status == ToolStatus.WAITING_APPROVAL.value:
                tool_status, action_status = ToolStatus.WAITING_APPROVAL, "waiting_approval"
            elif raw_status == ToolStatus.UNKNOWN.value:
                tool_status, action_status = ToolStatus.UNKNOWN, "unknown"
            elif value.get("ok") is False:
                tool_status, action_status = ToolStatus.FAILED, "failed"
            else:
                tool_status, action_status = ToolStatus.SUCCEEDED, "succeeded"
            result = ToolResult(
                status=tool_status, logical_action_id=logical_action_id, attempt_id=attempt["id"],
                job_id=str(value.get("job_id") or "") or None,
                output_refs=_refs(value), schema_refs=_schema_refs(value), preview=_preview(value),
                completeness=str(value.get("completeness") or value.get("result_completeness") or "unknown"),
                warnings=tuple(str(item) for item in value.get("warnings") or []),
                error_code=str(value.get("error_code") or "") or None,
                provenance_ref=str(value.get("provenance_ref") or "") or None,
                validation_status=str(value.get("validation_status") or "not_evaluated"),
            )
            self.store.finish_action(
                context.run_id, action["id"], attempt["id"], attempt["reservation_id"],
                lease_epoch=context.lease_epoch, status=action_status,
                result={"tool": tool_id, "value": value, "tool_result": result.to_dict()},
                error_code=result.error_code, external_job_id=result.job_id,
                actual_cost=float(value.get("actual_cost") or 1),
            )
            return ExecutedTool(result, value, events)
        except Exception as exc:
            error_code = _error_code(exc)
            value = {"ok": False, "tool": tool_id, "error": str(exc), "error_code": error_code}
            self.store.finish_action(
                context.run_id, action["id"], attempt["id"], attempt["reservation_id"],
                lease_epoch=context.lease_epoch, status="failed", result=value, error_code=error_code,
            )
            return ExecutedTool(ToolResult(
                status=ToolStatus.FAILED, logical_action_id=logical_action_id,
                attempt_id=attempt["id"], preview=value, error_code=error_code,
            ), value)


def _validate(value: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是对象")
    required = schema.get("required") or []
    missing = [str(name) for name in required if name not in value]
    if missing:
        raise ValueError(f"工具参数缺少字段：{', '.join(missing)}")
    properties = schema.get("properties") or {}
    for name, item in value.items():
        contract = properties.get(name)
        if not isinstance(contract, dict):
            continue
        expected = contract.get("type")
        if expected == "string" and not isinstance(item, str):
            raise ValueError(f"工具参数 {name} 必须是字符串")
        if expected == "array" and not isinstance(item, list):
            raise ValueError(f"工具参数 {name} 必须是数组")
        if expected == "object" and not isinstance(item, dict):
            raise ValueError(f"工具参数 {name} 必须是对象")
        if expected == "boolean" and not isinstance(item, bool):
            raise ValueError(f"工具参数 {name} 必须是布尔值")
        if expected in {"integer", "number"} and (not isinstance(item, (int, float)) or isinstance(item, bool)):
            raise ValueError(f"工具参数 {name} 必须是数字")
        if contract.get("enum") and item not in contract["enum"]:
            raise ValueError(f"工具参数 {name} 不在允许值范围内")


def _refs(value: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for key in ("dataset_ref_id", "result_id", "artifact_id", "chart_id", "manifest_id"):
        if value.get(key):
            found.append(str(value[key]))
    for key in ("output_refs", "artifact_ids"):
        found.extend(str(item) for item in value.get(key) or [])
    return tuple(dict.fromkeys(found))


def _schema_refs(value: dict[str, Any]) -> tuple[str, ...]:
    values = value.get("schema_refs") or ([value["schema_ref"]] if value.get("schema_ref") else [])
    return tuple(str(item) for item in values)


def _preview(value: dict[str, Any]) -> Any:
    if "preview" in value:
        return value["preview"]
    return {key: item for key, item in value.items() if key not in {"data", "content", "credential", "path"}}


def _error_code(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, TimeoutError):
        return "transient"
    if isinstance(exc, InterruptedError):
        return "cancelled"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_argument"
    if isinstance(exc, ConnectionError):
        return "transient"
    return "query_code_error"
