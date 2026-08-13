"""Conversations, turns, trace stages and feedback.

Trace is a queryable product surface, not a log file (spec 5.7), so stages are
rows with structured payloads rather than formatted text.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import META_SCHEMA, MetaBase


class ConversationRow(MetaBase):
    __tablename__ = "conversations"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(256), default="")
    dataset_name: Mapped[str] = mapped_column(String(64), default="")
    # Structured slots carried across turns (spec M-19), not chat history.
    slot_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    turns: Mapped[list["TurnRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class TurnRow(MetaBase):
    __tablename__ = "turns"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.conversations.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(Text)
    # answered | clarifying | refused | failed
    status: Mapped[str] = mapped_column(String(16), default="answered")
    answer: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Replay unit (spec 5.7): the intent as it stood when this turn ran.
    intent_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped[ConversationRow] = relationship(back_populates="turns")
    stages: Mapped[list["TraceStageRow"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )


class TraceStageRow(MetaBase):
    __tablename__ = "trace_stages"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.turns.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer)
    input_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    turn: Mapped[TurnRow] = relationship(back_populates="stages")


class FeedbackRow(MetaBase):
    """Thumbs-down attribution (spec M-38): the most honest source of eval data."""

    __tablename__ = "feedback"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.turns.id", ondelete="CASCADE"), index=True
    )
    is_positive: Mapped[bool] = mapped_column(default=True)
    # metric | time | sql | calculation | conclusion
    category: Mapped[str] = mapped_column(String(32), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VerifiedQueryRow(MetaBase):
    """A reviewed question-to-SQL pairing (spec M-20).

    A cache in front of the pipeline, not a separate architecture: hits still
    go through security rewriting before execution.
    """

    __tablename__ = "verified_queries"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_name: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(Text)
    normalized_question: Mapped[str] = mapped_column(Text, index=True)
    slot_signature: Mapped[str] = mapped_column(Text, index=True)
    fixed_sql: Mapped[str] = mapped_column(Text)
    intent_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )