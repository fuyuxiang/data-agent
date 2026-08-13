"""Semantic Revision state machine (S4 P2-02).

A Revision is an immutable snapshot of a dataset's semantic definition.
The state machine:

    draft -> linted -> approved -> published -> retired

Key invariants:
- A Revision is write-once. Once saved, only its state can change; the
  dataset_definition inside is frozen.
- At most one Revision per dataset is in `published` at any time.
- Modifying semantics = clone current published into a new draft, walk
  the state machine, and on publish the previous published moves to retired.
- Rollback re-publishes a retired revision: a new "publish" record points
  at the existing snapshot, never modifies it.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from app.semantic.model import DatasetDef


class RevisionState(str, Enum):
    """Lifecycle states of a semantic revision.

    Transitions:
    - (none) -> draft: a fresh revision is created in draft
    - draft -> linted: semantic lint passes
    - linted -> approved: semantic_approver signs off
    - approved -> published: published, prior published moves to retired
    - published -> retired: superseded by a newer published
    - retired -> published: rollback (re-publishes the snapshot)
    - any -> draft is forbidden (revisions are immutable)
    """

    DRAFT = "draft"
    LINTED = "linted"
    APPROVED = "approved"
    PUBLISHED = "published"
    RETIRED = "retired"


# Allowed forward transitions. The set of (from, to) pairs that may be
# applied. Anything not listed here raises InvalidTransition.
_ALLOWED_TRANSITIONS: frozenset[tuple[RevisionState, RevisionState]] = frozenset({
    (RevisionState.DRAFT, RevisionState.LINTED),
    (RevisionState.LINTED, RevisionState.APPROVED),
    (RevisionState.APPROVED, RevisionState.PUBLISHED),
    (RevisionState.PUBLISHED, RevisionState.RETIRED),
    (RevisionState.RETIRED, RevisionState.PUBLISHED),  # Rollback
})


class RevisionError(Exception):
    """Base class for revision errors."""


class InvalidTransitionError(RevisionError):
    """Attempted an illegal state transition."""

    def __init__(self, from_state: RevisionState, to_state: RevisionState):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition: {from_state.value!r} -> {to_state.value!r}"
        )


class ImmutableRevisionError(RevisionError):
    """Attempted to modify a revision's content (only state may change)."""


class AlreadyPublishedError(RevisionError):
    """Attempted to publish a revision when another is already published."""


@dataclass(frozen=True)
class LintReport:
    """Report from a semantic lint pass."""

    passed: bool
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiffEntry:
    """A single difference between two revisions."""

    kind: str  # "added" | "removed" | "modified" | "renamed"
    path: str  # JSON-pointer-ish path
    before: Any = None
    after: Any = None

    @property
    def is_breaking(self) -> bool:
        """Whether this diff entry is a breaking change."""
        return self.kind in ("removed", "modified") and not self._is_compat_extension()

    def _is_compat_extension(self) -> bool:
        """Some modifications are compatible (e.g., adding synonyms)."""
        return False


@dataclass(frozen=True)
class Revision:
    """An immutable snapshot of a dataset's semantic definition.

    The `dataset_definition` is the DatasetDef at the time this revision
    was created. It is never modified after construction.
    """

    id: int
    dataset_name: str
    parent_id: int | None  # Previous revision this was cloned from
    state: RevisionState
    dataset_definition: DatasetDef
    lint_report: LintReport | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    published_at: datetime | None = None
    retired_at: datetime | None = None
    notes: str = ""

    def with_state(self, new_state: RevisionState) -> "Revision":
        """Return a new Revision with state changed. Definition unchanged.

        Does NOT validate the transition — callers must do so via
        validate_transition() to surface a clean error.
        """
        # Replace fields except id, definition, parent
        new = Revision(
            id=self.id,
            dataset_name=self.dataset_name,
            parent_id=self.parent_id,
            state=new_state,
            dataset_definition=self.dataset_definition,
            lint_report=self.lint_report,
            approved_by=self.approved_by if new_state == RevisionState.APPROVED else self.approved_by,
            approved_at=self.approved_at if new_state == RevisionState.APPROVED else self.approved_at,
            created_at=self.created_at,
            published_at=datetime.utcnow() if new_state == RevisionState.PUBLISHED else self.published_at,
            retired_at=datetime.utcnow() if new_state == RevisionState.RETIRED else self.retired_at,
            notes=self.notes,
        )
        return new

    def mark_approved(self, approver: str) -> "Revision":
        """Set approved_by and approved_at, return new Revision."""
        return Revision(
            id=self.id,
            dataset_name=self.dataset_name,
            parent_id=self.parent_id,
            state=self.state,
            dataset_definition=self.dataset_definition,
            lint_report=self.lint_report,
            approved_by=approver,
            approved_at=datetime.utcnow(),
            created_at=self.created_at,
            published_at=self.published_at,
            retired_at=self.retired_at,
            notes=self.notes,
        )


def validate_transition(from_state: RevisionState, to_state: RevisionState) -> None:
    """Raise InvalidTransitionError if the transition is not allowed."""
    if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(from_state, to_state)


# --- Diff between revisions ------------------------------------------------

def diff_revisions(
    before: Revision, after: Revision
) -> tuple[DiffEntry, ...]:
    """Compute the diff between two revisions' dataset definitions.

    Returns a tuple of DiffEntry. The diff is structural: it walks
    fields and metrics, recording added/removed/modified items.
    """
    if before.dataset_name != after.dataset_name:
        raise RevisionError(
            f"Cannot diff revisions of different datasets: "
            f"{before.dataset_name!r} vs {after.dataset_name!r}"
        )

    before_def = before.dataset_definition
    after_def = after.dataset_definition

    entries: list[DiffEntry] = []

    # Diff fields
    before_fields = {f.name: f for f in before_def.fields}
    after_fields = {f.name: f for f in after_def.fields}

    for name in before_fields.keys() - after_fields.keys():
        entries.append(DiffEntry(
            kind="removed",
            path=f"fields[{name!r}]",
            before=before_fields[name].physical_column,
        ))

    for name in after_fields.keys() - before_fields.keys():
        entries.append(DiffEntry(
            kind="added",
            path=f"fields[{name!r}]",
            after=after_fields[name].physical_column,
        ))

    for name in before_fields.keys() & after_fields.keys():
        b, a = before_fields[name], after_fields[name]
        if b != a:
            entries.append(DiffEntry(
                kind="modified",
                path=f"fields[{name!r}]",
                before=b.physical_column,
                after=a.physical_column,
            ))

    # Diff metrics
    before_metrics = {m.name: m for m in before_def.metrics}
    after_metrics = {m.name: m for m in after_def.metrics}

    for name in before_metrics.keys() - after_metrics.keys():
        entries.append(DiffEntry(
            kind="removed",
            path=f"metrics[{name!r}]",
            before=before_metrics[name].source_field,
        ))

    for name in after_metrics.keys() - before_metrics.keys():
        entries.append(DiffEntry(
            kind="added",
            path=f"metrics[{name!r}]",
            after=after_metrics[name].source_field,
        ))

    for name in before_metrics.keys() & after_metrics.keys():
        b, a = before_metrics[name], after_metrics[name]
        if b != a:
            # Detect breaking changes (time_basis, source_field changes)
            entry = DiffEntry(
                kind="modified",
                path=f"metrics[{name!r}]",
                before={"source_field": b.source_field, "time_field": b.time_field},
                after={"source_field": a.source_field, "time_field": a.time_field},
            )
            entries.append(entry)

    return tuple(entries)


def has_breaking_changes(diff: tuple[DiffEntry, ...]) -> bool:
    """Return True if any diff entry is breaking.

    A diff is breaking when it removes a metric, modifies a metric's
    source_field, or modifies a metric's time_field.
    """
    for entry in diff:
        if entry.kind == "removed" and entry.path.startswith("metrics["):
            return True
        if entry.kind == "modified" and entry.path.startswith("metrics["):
            # Modifying source_field or time_field is breaking
            before = entry.before or {}
            after = entry.after or {}
            if before.get("source_field") != after.get("source_field"):
                return True
            if before.get("time_field") != after.get("time_field"):
                return True
        if entry.kind == "modified" and entry.path.startswith("fields["):
            # Renaming a physical column is breaking
            return True
    return False


# --- Cloning for new revisions --------------------------------------------

def clone_for_draft(
    parent: Revision,
    new_id: int,
    new_definition: DatasetDef,
) -> Revision:
    """Clone a published revision into a new draft with a new definition.

    The new revision starts in draft state, references the parent for
    lineage, and carries a fresh created_at.
    """
    return Revision(
        id=new_id,
        dataset_name=parent.dataset_name,
        parent_id=parent.id,
        state=RevisionState.DRAFT,
        dataset_definition=new_definition,
        lint_report=None,
        approved_by=None,
        approved_at=None,
        created_at=datetime.utcnow(),
        published_at=None,
        retired_at=None,
        notes=f"Cloned from revision {parent.id}",
    )
