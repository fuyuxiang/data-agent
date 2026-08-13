"""Stage 1: Verified Query recall (spec M-20).

Two match paths, tried in order of confidence:

1. normalized question text — the same question asked again
2. slot signature — a different phrasing of the same query

Slot matching is why the signature excludes confidence and raw question: two
wordings of one query must collide.
"""

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intent.schema import QueryIntent
from app.observability.orm import VerifiedQueryRow

_PUNCTUATION = re.compile(r"[\s，。？！、,.?!;；:：'\"“”‘’()（）\[\]【】-]+")


@dataclass(frozen=True, slots=True)
class VerifiedHit:
    id: int
    question: str
    fixed_sql: str
    match_kind: str


def normalize_question(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCTUATION.sub("", folded)


def _to_hit(row: VerifiedQueryRow, match_kind: str) -> VerifiedHit:
    row.hit_count += 1
    return VerifiedHit(
        id=row.id, question=row.question, fixed_sql=row.fixed_sql, match_kind=match_kind
    )


def recall(
    session: Session,
    dataset_name: str,
    question: str,
    intent: QueryIntent | None = None,
) -> VerifiedHit | None:
    base = select(VerifiedQueryRow).where(
        VerifiedQueryRow.dataset_name == dataset_name,
        VerifiedQueryRow.is_active.is_(True),
    )

    exact = session.execute(
        base.where(VerifiedQueryRow.normalized_question == normalize_question(question))
        .order_by(VerifiedQueryRow.id)
        .limit(1)
    ).scalar_one_or_none()
    if exact is not None:
        return _to_hit(exact, "question")

    if intent is None:
        return None

    by_slots = session.execute(
        base.where(VerifiedQueryRow.slot_signature == intent.slot_signature())
        .order_by(VerifiedQueryRow.id)
        .limit(1)
    ).scalar_one_or_none()
    if by_slots is not None:
        return _to_hit(by_slots, "slots")

    return None


def register(
    session: Session,
    *,
    dataset_name: str,
    question: str,
    fixed_sql: str,
    intent: QueryIntent,
    created_by: str,
) -> VerifiedQueryRow:
    row = VerifiedQueryRow(
        dataset_name=dataset_name,
        question=question,
        normalized_question=normalize_question(question),
        slot_signature=intent.slot_signature(),
        fixed_sql=fixed_sql,
        intent_snapshot=intent.to_payload(),
        created_by=created_by,
    )
    session.add(row)
    return row