from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .contracts import ModelResponse, ModelToolCall


class ModelProtocolError(RuntimeError):
    pass


class ModelAdapter(Protocol):
    protocol: str
    model: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        on_text_delta: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ModelResponse: ...


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _usage(value: Any) -> dict[str, int]:
    prompt = int(_value(value, "prompt_tokens", _value(value, "input_tokens", 0)) or 0)
    completion = int(_value(value, "completion_tokens", _value(value, "output_tokens", 0)) or 0)
    total = int(_value(value, "total_tokens", prompt + completion) or prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _arguments(raw: Any, call_id: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise ModelProtocolError(f"工具调用 {call_id} 的 JSON 参数不完整或无效") from exc
    if not isinstance(parsed, dict):
        raise ModelProtocolError(f"工具调用 {call_id} 的参数必须是 JSON 对象")
    return parsed


class ChatCompletionsAdapter:
    protocol = "openai_chat_completions"

    def __init__(self, client: Any, provider: dict[str, Any]):
        self.client = client
        self.provider = provider
        self.model = str(provider["model"])

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        on_text_delta: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        request = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.provider.get("temperature", 0.2)),
            "max_tokens": max(1, int(max_output_tokens)),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request.update({"tools": tools, "tool_choice": "auto"})
        if self.provider.get("enable_thinking"):
            request["reasoning_effort"] = self.provider.get("reasoning_effort", "medium")
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as first_error:
            if "stream_options" not in str(first_error).lower() and not isinstance(first_error, TypeError):
                raise
            request.pop("stream_options", None)
            response = self.client.chat.completions.create(**request)
        content: list[str] = []
        refusal: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage_value: Any = None
        finish_reason = ""
        try:
            for chunk in response:
                if should_cancel and should_cancel():
                    raise InterruptedError("模型调用已取消")
                if _value(chunk, "usage") is not None:
                    usage_value = _value(chunk, "usage")
                choices = _value(chunk, "choices", []) or []
                if not choices:
                    continue
                choice = choices[0]
                if _value(choice, "finish_reason"):
                    finish_reason = str(_value(choice, "finish_reason"))
                delta = _value(choice, "delta")
                if delta is None:
                    continue
                text = _value(delta, "content")
                if text:
                    content.append(str(text))
                    if on_text_delta:
                        on_text_delta(str(text))
                refused = _value(delta, "refusal")
                if refused:
                    refusal.append(str(refused))
                for raw_call in _value(delta, "tool_calls", []) or []:
                    index = int(_value(raw_call, "index", 0) or 0)
                    current = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if _value(raw_call, "id"):
                        current["id"] = str(_value(raw_call, "id"))
                    function = _value(raw_call, "function")
                    if function is not None:
                        if _value(function, "name"):
                            current["name"] += str(_value(function, "name"))
                        if _value(function, "arguments"):
                            current["arguments"] += str(_value(function, "arguments"))
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        normalized_calls = []
        for index, raw in sorted(calls.items()):
            if not raw["name"]:
                raise ModelProtocolError("模型返回了没有工具名的调用")
            call_id = raw["id"] or f"call_{index}_{int(time.time() * 1000)}"
            normalized_calls.append(ModelToolCall(call_id, raw["name"], _arguments(raw["arguments"], call_id)))
        return ModelResponse(
            protocol=self.protocol, model=self.model, content="".join(content).strip(),
            tool_calls=tuple(normalized_calls), finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
            refusal="".join(refusal).strip() or None, usage=_usage(usage_value),
        )


class ResponsesAdapter:
    """Native OpenAI Responses protocol adapter (not a base_url alias)."""

    protocol = "openai_responses"

    def __init__(self, client: Any, provider: dict[str, Any]):
        self.client = client
        self.provider = provider
        self.model = str(provider["model"])

    @staticmethod
    def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for item in tools:
            function = item.get("function") or {}
            output.append({
                "type": "function", "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                "strict": False,
            })
        return output

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        on_text_delta: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ModelResponse:
        if should_cancel and should_cancel():
            raise InterruptedError("模型调用已取消")
        response = self.client.responses.create(
            model=self.model,
            input=messages,
            tools=self._tools(tools),
            max_output_tokens=max(1, int(max_output_tokens)),
            temperature=float(self.provider.get("temperature", 0.2)),
        )
        content: list[str] = []
        refusal: list[str] = []
        calls: list[ModelToolCall] = []
        for item in _value(response, "output", []) or []:
            item_type = str(_value(item, "type", ""))
            if item_type == "function_call":
                call_id = str(_value(item, "call_id", _value(item, "id", "")) or "")
                calls.append(ModelToolCall(
                    call_id or f"call_{len(calls)}_{int(time.time() * 1000)}",
                    str(_value(item, "name", "")),
                    _arguments(_value(item, "arguments", "{}"), call_id),
                ))
                continue
            for part in _value(item, "content", []) or []:
                part_type = str(_value(part, "type", ""))
                text = _value(part, "text")
                if part_type in {"output_text", "text"} and text:
                    content.append(str(text))
                    if on_text_delta:
                        on_text_delta(str(text))
                if part_type == "refusal" and text:
                    refusal.append(str(text))
        status = str(_value(response, "status", "completed"))
        incomplete = _value(response, "incomplete_details")
        finish_reason = "length" if incomplete else ("tool_calls" if calls else status)
        return ModelResponse(
            protocol=self.protocol, model=self.model, content="".join(content).strip(),
            tool_calls=tuple(calls), finish_reason=finish_reason,
            refusal="".join(refusal).strip() or None, usage=_usage(_value(response, "usage")),
        )


class ScriptedModelAdapter:
    """Deterministic protocol fixture; never used as a production fallback."""

    protocol = "scripted_test"

    def __init__(self, responses: Iterable[ModelResponse], model: str = "scripted"):
        self.responses = iter(responses)
        self.model = model

    def complete(self, _messages, _tools, *, max_output_tokens, on_text_delta=None, should_cancel=None):
        if should_cancel and should_cancel():
            raise InterruptedError("模型调用已取消")
        response = next(self.responses)
        if response.content and on_text_delta:
            on_text_delta(response.content)
        return response


def build_model_adapter(client: Any, provider: dict[str, Any]) -> ModelAdapter:
    protocol = str(provider.get("protocol") or "chat_completions").lower().replace("-", "_")
    if protocol in {"responses", "openai_responses"}:
        return ResponsesAdapter(client, provider)
    if protocol in {"chat", "chat_completions", "openai_chat_completions"}:
        return ChatCompletionsAdapter(client, provider)
    raise ValueError(f"不支持的模型协议：{protocol}")
