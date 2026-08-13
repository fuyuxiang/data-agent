"""Stage 3: value mapping, ambiguity detection and clarification.

Two responsibilities that belong together because both decide whether the
query may proceed: mapping spoken values to physical ones via the dictionary,
and turning low confidence into questions rather than guesses.
"""

from dataclasses import dataclass

from app.core.config import Settings
from app.intent.schema import FilterCondition, IntentKind, QueryIntent
from app.pipeline.clarify import ClarifyKind, ClarifyOption, ClarifyRequest, default_assumption
from app.semantic.enums import SemanticType
from app.semantic.model import DatasetDef


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    intent: QueryIntent
    clarifications: tuple[ClarifyRequest, ...] = ()
    assumptions: tuple[str, ...] = ()


def _metric_options(dataset: DatasetDef) -> tuple[ClarifyOption, ...]:
    return tuple(
        ClarifyOption(
            value=metric.name,
            label=metric.business_name,
            hint=metric.description or f"版本 v{metric.version}",
        )
        for metric in dataset.metrics
    )


def _time_options() -> tuple[ClarifyOption, ...]:
    return (
        ClarifyOption(value="this_month", label="本月"),
        ClarifyOption(value="last_month", label="上月"),
        ClarifyOption(value="last_7_days", label="最近 7 天"),
        ClarifyOption(value="last_30_days", label="最近 30 天"),
    )


def _enum_options(dataset: DatasetDef, field_name: str) -> tuple[ClarifyOption, ...]:
    field = dataset.field(field_name)
    return tuple(
        ClarifyOption(value=item.physical_value, label=item.business_value, hint=item.description)
        for item in field.enum_values
    )


def _resolve_filters(
    dataset: DatasetDef, intent: QueryIntent
) -> tuple[list[FilterCondition], list[ClarifyRequest]]:
    resolved: list[FilterCondition] = []
    requests: list[ClarifyRequest] = []

    for condition in intent.filters:
        field = dataset.field(condition.field)
        if field.semantic_type != SemanticType.ENUM:
            # Numbers and dates need no dictionary; keep what the user said.
            resolved.append(condition.model_copy(update={"values": list(condition.spoken_values)}))
            continue

        physical: list[str] = []
        unmapped: list[str] = []
        for spoken in condition.spoken_values:
            match = dataset.resolve_enum(field.name, spoken)
            if match is None:
                unmapped.append(spoken)
            else:
                physical.append(match)

        if unmapped:
            requests.append(
                ClarifyRequest(
                    kind=ClarifyKind.ENTITY,
                    target=f"filters.{field.name}",
                    question=(
                        f"「{'、'.join(unmapped)}」在{field.business_name or field.name}中"
                        "找不到对应取值，你指的是哪个？"
                    ),
                    options=_enum_options(dataset, field.name),
                )
            )
            continue

        resolved.append(condition.model_copy(update={"values": physical}))

    return resolved, requests


def _confidence_requests(
    dataset: DatasetDef, intent: QueryIntent, threshold: float
) -> list[ClarifyRequest]:
    requests: list[ClarifyRequest] = []
    confidence = intent.confidence

    metric_score = confidence.metric if confidence.metric is not None else confidence.overall
    if not intent.metrics or metric_score < threshold:
        requests.append(
            ClarifyRequest(
                kind=ClarifyKind.METRIC,
                target="metrics",
                question="你想看哪个指标？",
                options=_metric_options(dataset),
            )
        )

    time_score = confidence.time if confidence.time is not None else confidence.overall
    if intent.time is None or time_score < threshold:
        requests.append(
            ClarifyRequest(
                kind=ClarifyKind.TIME,
                target="time",
                question="你想看哪个时间范围？",
                options=_time_options(),
            )
        )

    if intent.dimensions:
        dimension_score = (
            confidence.dimension if confidence.dimension is not None else confidence.overall
        )
        if dimension_score < threshold:
            requests.append(
                ClarifyRequest(
                    kind=ClarifyKind.DIMENSION,
                    target="dimensions",
                    question="你想按哪个维度分组？",
                    options=tuple(
                        ClarifyOption(
                            value=field.name, label=field.business_name or field.name
                        )
                        for field in dataset.fields
                        if field.is_groupable
                    ),
                )
            )

    return requests


def _apply_defaults(
    intent: QueryIntent, requests: list[ClarifyRequest]
) -> tuple[QueryIntent, list[str]]:
    """Round limit reached: fill slots with the first option, stating each one."""
    assumptions: list[str] = []
    updated = intent

    for request in requests:
        decision = default_assumption(request)
        if decision is None:
            continue
        target, message = decision
        assumptions.append(message)

        if target == "metrics" and not updated.metrics:
            updated = updated.model_copy(update={"metrics": [request.options[0].value]})

    return updated, assumptions


def resolve_intent(
    dataset: DatasetDef,
    intent: QueryIntent,
    settings: Settings,
    *,
    round_index: int = 0,
) -> ResolveOutcome:
    if intent.kind == IntentKind.UNSUPPORTED:
        # Out of scope: refused upstream, never clarified into shape.
        return ResolveOutcome(intent=intent, assumptions=tuple(intent.assumptions))

    filters, entity_requests = _resolve_filters(dataset, intent)
    resolved = intent.model_copy(update={"filters": filters})

    requests = entity_requests + _confidence_requests(
        dataset, resolved, settings.clarify_confidence_threshold
    )
    assumptions = list(intent.assumptions)

    if requests and round_index >= settings.clarify_max_rounds:
        resolved, defaults = _apply_defaults(resolved, requests)
        return ResolveOutcome(
            intent=resolved, clarifications=(), assumptions=tuple(assumptions + defaults)
        )

    return ResolveOutcome(
        intent=resolved, clarifications=tuple(requests), assumptions=tuple(assumptions)
    )