from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FEEDBACK_CATEGORIES = ("metric", "time", "sql", "calculation", "conclusion")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    dataset_name: str
    conversation_id: int | None = None


class CitationLineOut(BaseModel):
    label: str
    value: str
    source: Literal["user", "permission"]


class CitationOut(BaseModel):
    metric: str
    time: str
    filters: list[CitationLineOut] = Field(default_factory=list)
    data_updated_at: str = ""


class DrillDownOut(BaseModel):
    label: str
    kind: str
    target: str


class AnswerOut(BaseModel):
    headline: str
    conclusion: str = ""
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    citation: CitationOut | None = None
    drill_downs: list[DrillDownOut] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class ClarifyOptionOut(BaseModel):
    value: str
    label: str
    hint: str = ""


class ClarifyOut(BaseModel):
    kind: str
    target: str
    question: str
    options: list[ClarifyOptionOut] = Field(default_factory=list)


class AskOut(BaseModel):
    status: Literal["answered", "clarifying", "refused", "failed"]
    conversation_id: int
    turn_id: int
    answer: AnswerOut | None = None
    clarifications: list[ClarifyOut] = Field(default_factory=list)
    refusal_reason: str = ""
    slot_state: dict[str, Any] | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    dataset_name: str
    updated_at: datetime


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    status: str
    answer: dict[str, Any] | None = None
    created_at: datetime


class FeedbackIn(BaseModel):
    is_positive: bool
    category: str = ""
    comment: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _check_category(self) -> "FeedbackIn":
        """M-38: a thumbs-down without attribution is not usable eval data."""
        if self.is_positive:
            return self
        if self.category not in _FEEDBACK_CATEGORIES:
            raise ValueError(f"负反馈必须归因到 {_FEEDBACK_CATEGORIES} 之一")
        return self


class TraceStageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage: str
    sequence: int
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    error: str | None = ""


class TraceOut(BaseModel):
    turn_id: int
    question: str
    status: str
    intent_snapshot: dict[str, Any] | None = None
    stages: list[TraceStageOut] = Field(default_factory=list)


class ReplayOut(BaseModel):
    sql: str
    display_sql: str
    matches_original: bool
    applied_row_filters: list[str] = Field(default_factory=list)
    masked_field_names: list[str] = Field(default_factory=list)