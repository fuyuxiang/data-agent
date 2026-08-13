"""Stage-by-stage tracing.

The recorder owns sequence numbering so stages cannot be recorded out of order,
and `stage_timer` guarantees a failing stage is still written — the failures are
precisely what Trace exists to explain.
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.orm import TraceStageRow


class Stage(str, Enum):
    VERIFIED_RECALL = "verified_recall"
    INTENT = "intent"
    SEMANTIC_RESOLVE = "semantic_resolve"
    COMPILE = "compile"
    SECURITY = "security"
    EXECUTE = "execute"
    ANSWER = "answer"


@dataclass
class StageSpan:
    """Mutable handle a stage uses to report what it produced."""

    output: dict[str, Any] | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    def __init__(self, session: Session, turn_id: int) -> None:
        self._session = session
        self._turn_id = turn_id
        self._sequence = 0

    def record(
        self,
        stage: Stage,
        *,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any] | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        elapsed_ms: int = 0,
        error: str | None = None,
    ) -> TraceStageRow:
        self._sequence += 1
        row = TraceStageRow(
            turn_id=self._turn_id,
            stage=stage.value,
            sequence=self._sequence,
            input_payload=input_payload,
            output_payload=output_payload,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            error=error,
        )
        self._session.add(row)
        return row

    @contextmanager
    def stage_timer(self, stage: Stage, input_payload: dict[str, Any]) -> Iterator[StageSpan]:
        span = StageSpan()
        started = time.perf_counter()
        try:
            yield span
        except Exception as error:
            self.record(
                stage,
                input_payload=input_payload,
                model=span.model,
                prompt_tokens=span.prompt_tokens,
                completion_tokens=span.completion_tokens,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=f"{error.__class__.__name__}: {error}",
            )
            raise
        self.record(
            stage,
            input_payload=input_payload,
            output_payload=span.output,
            model=span.model,
            prompt_tokens=span.prompt_tokens,
            completion_tokens=span.completion_tokens,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def load_trace(session: Session, turn_id: int) -> list[TraceStageRow]:
    statement = (
        select(TraceStageRow)
        .where(TraceStageRow.turn_id == turn_id)
        .order_by(TraceStageRow.sequence)
    )
    return list(session.execute(statement).scalars())