"""LLM call plus the validation layer that contains it.

Everything the model returns is checked against the semantic model before it
becomes a QueryIntent: a hallucinated metric name that reached the compiler
would either crash or, worse, resolve to something close but wrong.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.intent.prompt import build_intent_prompt
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    SortSpec,
    TimeGrain,
    TimeRange,
)
from app.llm.structured import schema_from_model
from app.semantic.model import DatasetDef

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_SQL_TOKENS = re.compile(
    r"\b(select|insert|update|delete|drop|from\s+\w+|join|group\s+by)\b", re.IGNORECASE
)


class IntentRecognitionError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LlmClient(Protocol):
    def complete(self, system: str, user: str) -> LlmCompletion: ...


class _FilterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(alias="field")
    operator: FilterOperator = FilterOperator.IN
    spoken_values: list[str] = Field(default_factory=list)


class _TimeExpressionPayload(BaseModel):
    """Relative time expression from the model.

    The model outputs relative time (offset, unit) rather than absolute dates.
    TimeResolver in orchestrator will convert this to concrete dates later.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str  # "relative" | "absolute" | "range" | "none"
    unit: str | None = None  # "day" | "week" | "month" | "quarter" | "year"
    offset: int = 0
    to_date: bool = False
    start_date: str | None = None  # ISO format, for kind=absolute/range
    end_date: str | None = None
    expression: str = ""  # user's original phrasing


class _SortPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str
    descending: bool = True
    limit: int | None = None


class _ConfidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall: float
    metric: float | None = None
    time: float | None = None
    dimension: float | None = None
    filter: float | None = None


class IntentPayload(BaseModel):
    """Shape contract for the model's output."""

    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[_FilterPayload] = Field(default_factory=list)
    time_expression: _TimeExpressionPayload | None = None  # relative time from model
    comparison: ComparisonKind = ComparisonKind.NONE
    sort: _SortPayload | None = None
    confidence: _ConfidencePayload
    assumptions: list[str] = Field(default_factory=list)


def _extract_json(content: str) -> dict:
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise IntentRecognitionError(f"模型输出不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise IntentRecognitionError("模型输出不是 JSON 对象")
    return payload


def _assert_no_sql(content: str) -> None:
    if _SQL_TOKENS.search(content):
        raise IntentRecognitionError("模型输出中出现 SQL 片段，已拒绝采用")


def _assert_known_references(dataset: DatasetDef, payload: IntentPayload) -> None:
    for name in payload.metrics:
        if not dataset.has_metric(name):
            raise IntentRecognitionError(f"模型返回了不存在的指标 {name}")
    for name in payload.dimensions:
        if not dataset.has_field(name):
            raise IntentRecognitionError(f"模型返回了不存在的维度 {name}")
    for item in payload.filters:
        if not dataset.has_field(item.field_name):
            raise IntentRecognitionError(f"模型返回了不存在的过滤字段 {item.field_name}")
    if payload.sort and not (
        dataset.has_metric(payload.sort.by) or dataset.has_field(payload.sort.by)
    ):
        raise IntentRecognitionError(f"模型返回了不存在的排序字段 {payload.sort.by}")


def _to_intent(dataset: DatasetDef, payload: IntentPayload, question: str) -> QueryIntent:
    """Convert IntentPayload to QueryIntent.

    Time expressions are stored but not converted yet; TimeResolver in the
    orchestrator will convert relative expressions to concrete dates later.
    """
    time_range = None
    # TODO (S3): TimeResolver will convert time_expression to concrete dates
    # For now, relative time expressions are accepted but not converted
    if payload.time_expression is not None and payload.time_expression.kind != "none":
        # If model provided absolute dates, we can use them now
        if payload.time_expression.kind == "absolute":
            # Use the model's unit as grain (day/week/month/...) so the
            # downstream compiler and citation match the user's intent
            # ("本月" → grain=month), not always day.
            try:
                grain = TimeGrain(payload.time_expression.unit or "day")
            except ValueError:
                grain = TimeGrain.DAY
            try:
                time_range = TimeRange(
                    start=date.fromisoformat(payload.time_expression.start_date),
                    end=date.fromisoformat(
                        payload.time_expression.end_date
                        or payload.time_expression.start_date
                    ),
                    grain=grain,
                    expression=payload.time_expression.expression,
                )
            except (ValueError, TypeError) as error:
                raise IntentRecognitionError(f"模型返回的绝对日期无法解析：{error}") from error
        else:
            # For relative expressions (kind=relative), create a placeholder
            # that will be resolved later by TimeResolver
            # Use expression as a marker for now
            time_range = TimeRange(
                start=date(2026, 1, 1),  # placeholder
                end=date(2026, 1, 1),
                grain=TimeGrain.DAY,
                expression=f"{payload.time_expression.kind}:{payload.time_expression.unit}:{payload.time_expression.offset}:{payload.time_expression.expression}",
            )

    confidence_kwargs: dict[str, float] = {"overall": payload.confidence.overall}
    for slot in ("metric", "time", "dimension", "filter"):
        value = getattr(payload.confidence, slot)
        if value is not None:
            confidence_kwargs[slot] = value

    return QueryIntent(
        kind=payload.kind,
        dataset=dataset.name,
        metrics=list(payload.metrics),
        dimensions=list(payload.dimensions),
        filters=[
            FilterCondition(
                field=item.field_name,
                operator=item.operator,
                values=[],
                spoken_values=list(item.spoken_values),
            )
            for item in payload.filters
        ],
        time=time_range,
        comparison=payload.comparison,
        sort=(
            SortSpec(
                by=payload.sort.by,
                descending=payload.sort.descending,
                limit=payload.sort.limit,
            )
            if payload.sort
            else None
        ),
        confidence=FieldConfidence(**confidence_kwargs),
        assumptions=list(payload.assumptions),
        raw_question=question,
    )


def recognize(
    client: LlmClient,
    dataset: DatasetDef,
    question: str,
    slot_state: dict | None = None,
    *,
    full_dataset: DatasetDef | None = None,
    recorder=None,  # Optional TraceRecorder for Trace recording
) -> tuple[QueryIntent, LlmCompletion]:
    """Call the LLM and validate its output.

    The prompt is always built from ``dataset`` (the permission view). Reference
    validation uses ``full_dataset`` when supplied, so a hallucinated metric
    name that exists in the full catalogue but is hidden from the user is
    distinguished from one that was invented wholesale — the orchestrator
    handles the former as a permission refusal.

    If ``recorder`` is provided, LLM call is wrapped in stage_timer to record
    model snapshot and latency.
    """
    # If recorder provided, wrap LLM call in stage_timer
    if recorder is not None:
        from app.observability.trace import Stage

        with recorder.stage_timer(
            Stage.INTENT,
            input_payload={"question": question, "slot_state": slot_state}
        ) as span:
            system, user = build_intent_prompt(dataset, question, slot_state)
            completion = client.complete(system, user)

            # Record LLM snapshot
            span.model = completion.model
            span.prompt_tokens = completion.prompt_tokens
            span.completion_tokens = completion.completion_tokens
            span.output = {"model": completion.model}
    else:
        # No recording, just call directly
        system, user = build_intent_prompt(dataset, question, slot_state)
        completion = client.complete(system, user)

    _assert_no_sql(completion.content)
    raw = _extract_json(completion.content)
    try:
        payload = IntentPayload.model_validate(raw)
    except ValidationError as error:
        raise IntentRecognitionError(f"模型输出结构不符合意图 Schema：{error}") from error

    _assert_known_references(full_dataset or dataset, payload)
    return _to_intent(dataset, payload, question), completion


class OpenAiCompatClient:
    """Production client against an OpenAI-compatible endpoint.

    Uses Structured Outputs (response_format: json_schema) for strict validation.
    """

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds

    def complete(self, system: str, user: str) -> LlmCompletion:
        """Call OpenAI with Structured Outputs for strict JSON schema validation."""
        # Generate JSON schema from IntentPayload
        schema = schema_from_model(IntentPayload)

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Intent recognition must be as reproducible as the API allows.
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "IntentPayload",
                    "schema": schema,
                    "strict": True,
                },
            },
            timeout=self._timeout,
        )
        usage = response.usage
        return LlmCompletion(
            content=response.choices[0].message.content or "",
            model=response.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )