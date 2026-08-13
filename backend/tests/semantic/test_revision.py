"""Tests for Semantic Revision state machine (S4 P2-02)."""

import pytest
from datetime import datetime


pytestmark = pytest.mark.no_db


def _make_dataset(*, name: str = "orders", field_count: int = 2, metric_count: int = 1):
    """Build a sample dataset for testing."""
    from app.semantic.model import DatasetDef, FieldDef, MetricDef

    fields = tuple(
        FieldDef(
            name=f"f{i}",
            business_name=f"f{i}",
            physical_column=f"col_{i}",
            semantic_type="text" if i > 0 else "measure",
            is_groupable=(i == 0),
        )
        for i in range(field_count)
    )

    metrics = tuple(
        MetricDef(
            name=f"m{i}",
            business_name=f"m{i}",
            description="",
            kind="atomic",
            time_field="t",
            version=1,
            source_field=f"f{i}" if i < field_count else "f0",
            aggregation="sum",
        )
        for i in range(metric_count)
    )

    return DatasetDef(
        name=name,
        business_name=name,
        grain="day",
        applicable_scenario="",
        forbidden_scenario="",
        physical_table=f"sample.{name}",
        is_published=False,
        metrics=metrics,
        fields=fields,
    )


def _make_revision(
    state="draft",
    dataset=None,
    id=1,
    dataset_name="orders",
    field_count: int = 2,
    metric_count: int = 1,
):
    """Build a sample revision."""
    from app.semantic.revision import Revision, RevisionState, LintReport

    if dataset is None:
        dataset = _make_dataset(
            name=dataset_name, field_count=field_count, metric_count=metric_count
        )

    return Revision(
        id=id,
        dataset_name=dataset_name,
        parent_id=None,
        state=RevisionState(state),
        dataset_definition=dataset,
        lint_report=LintReport(passed=True) if state != "draft" else None,
    )


class TestRevisionCreation:
    """Test revision creation and immutability."""

    def test_revision_constructs(self):
        """Revision with required fields constructs."""
        from app.semantic.revision import Revision, RevisionState

        r = Revision(
            id=1,
            dataset_name="orders",
            parent_id=None,
            state=RevisionState.DRAFT,
            dataset_definition=_make_dataset(),
        )

        assert r.id == 1
        assert r.state == RevisionState.DRAFT

    def test_revision_definition_immutable(self):
        """dataset_definition cannot be modified after construction."""
        from app.semantic.revision import Revision, RevisionState

        dataset = _make_dataset()
        r = Revision(
            id=1,
            dataset_name="orders",
            parent_id=None,
            state=RevisionState.DRAFT,
            dataset_definition=dataset,
        )

        # Frozen dataclass: assigning to a field raises FrozenInstanceError
        with pytest.raises(Exception):
            r.dataset_definition = _make_dataset(name="other")  # type: ignore


class TestStateTransitions:
    """Test state machine transitions."""

    def test_draft_to_linted_allowed(self):
        """draft -> linted is allowed."""
        from app.semantic.revision import validate_transition, RevisionState

        # Should not raise
        validate_transition(RevisionState.DRAFT, RevisionState.LINTED)

    def test_linted_to_approved_allowed(self):
        """linted -> approved is allowed."""
        from app.semantic.revision import validate_transition, RevisionState

        validate_transition(RevisionState.LINTED, RevisionState.APPROVED)

    def test_approved_to_published_allowed(self):
        """approved -> published is allowed."""
        from app.semantic.revision import validate_transition, RevisionState

        validate_transition(RevisionState.APPROVED, RevisionState.PUBLISHED)

    def test_published_to_retired_allowed(self):
        """published -> retired is allowed."""
        from app.semantic.revision import validate_transition, RevisionState

        validate_transition(RevisionState.PUBLISHED, RevisionState.RETIRED)

    def test_retired_to_published_allowed_for_rollback(self):
        """retired -> published is allowed (rollback)."""
        from app.semantic.revision import validate_transition, RevisionState

        validate_transition(RevisionState.RETIRED, RevisionState.PUBLISHED)

    def test_draft_to_published_forbidden(self):
        """Cannot skip intermediate states."""
        from app.semantic.revision import (
            InvalidTransitionError, validate_transition, RevisionState,
        )

        with pytest.raises(InvalidTransitionError):
            validate_transition(RevisionState.DRAFT, RevisionState.PUBLISHED)

    def test_linted_to_draft_forbidden(self):
        """Cannot go back to draft."""
        from app.semantic.revision import (
            InvalidTransitionError, validate_transition, RevisionState,
        )

        with pytest.raises(InvalidTransitionError):
            validate_transition(RevisionState.LINTED, RevisionState.DRAFT)

    def test_published_to_approved_forbidden(self):
        """Cannot go back from published to approved."""
        from app.semantic.revision import (
            InvalidTransitionError, validate_transition, RevisionState,
        )

        with pytest.raises(InvalidTransitionError):
            validate_transition(RevisionState.PUBLISHED, RevisionState.APPROVED)


class TestRevisionStateChange:
    """Test revision state changes via with_state()."""

    def test_with_state_returns_new_revision(self):
        """with_state returns a new revision (immutability)."""
        from app.semantic.revision import Revision, RevisionState

        original = _make_revision(state="draft")
        updated = original.with_state(RevisionState.LINTED)

        # Original unchanged
        assert original.state == RevisionState.DRAFT
        # New revision has new state
        assert updated.state == RevisionState.LINTED

    def test_with_state_preserves_definition(self):
        """State change preserves the dataset definition."""
        from app.semantic.revision import RevisionState

        original = _make_revision(state="draft")
        updated = original.with_state(RevisionState.LINTED)

        assert updated.dataset_definition is original.dataset_definition

    def test_with_state_published_records_timestamp(self):
        """Transitioning to published records published_at."""
        from app.semantic.revision import RevisionState

        original = _make_revision(state="approved")
        assert original.published_at is None

        updated = original.with_state(RevisionState.PUBLISHED)
        assert updated.published_at is not None

    def test_with_state_retired_records_timestamp(self):
        """Transitioning to retired records retired_at."""
        from app.semantic.revision import RevisionState

        original = _make_revision(state="published")
        assert original.retired_at is None

        updated = original.with_state(RevisionState.RETIRED)
        assert updated.retired_at is not None


class TestRevisionApproval:
    """Test approval workflow."""

    def test_mark_approved_records_approver(self):
        """mark_approved records approver name and time."""
        original = _make_revision(state="linted")
        assert original.approved_by is None

        approved = original.mark_approved("alice@example.com")
        assert approved.approved_by == "alice@example.com"
        assert approved.approved_at is not None

    def test_mark_approved_preserves_state(self):
        """mark_approved does not change state (caller does)."""
        original = _make_revision(state="linted")
        approved = original.mark_approved("alice")

        # State unchanged
        assert approved.state == original.state


class TestDiffRevisions:
    """Test revision diff calculation."""

    def test_diff_added_field(self):
        """Adding a field appears in diff."""
        from app.semantic.revision import diff_revisions

        rev1 = _make_revision(state="published", field_count=1)
        rev2 = _make_revision(state="draft", field_count=2, id=2)

        diff = diff_revisions(rev1, rev2)

        # One new field
        added = [d for d in diff if d.kind == "added"]
        assert len(added) == 1
        assert "fields" in added[0].path

    def test_diff_removed_metric(self):
        """Removing a metric appears in diff."""
        from app.semantic.revision import diff_revisions

        rev1 = _make_revision(state="published", metric_count=2, id=1)
        rev2 = _make_revision(state="draft", metric_count=1, id=2)

        diff = diff_revisions(rev1, rev2)

        # One removed metric
        removed = [d for d in diff if d.kind == "removed" and "metrics" in d.path]
        assert len(removed) == 1
        assert removed[0].path == "metrics['m1']"

    def test_diff_modified_metric(self):
        """Modifying a metric appears in diff."""
        from app.semantic.model import DatasetDef, FieldDef, MetricDef
        from app.semantic.revision import diff_revisions, Revision, RevisionState

        fields = (FieldDef(name="amount", business_name="a", physical_column="amount", semantic_type="measure"),)
        m1 = MetricDef(name="m", business_name="m", description="",
                       kind="atomic", time_field="t1", source_field="amount", aggregation="sum")
        m2 = MetricDef(name="m", business_name="m", description="",
                       kind="atomic", time_field="t2",  # Changed time field!
                       source_field="amount", aggregation="sum")

        rev1 = Revision(id=1, dataset_name="d", parent_id=None, state=RevisionState.PUBLISHED,
                        dataset_definition=DatasetDef(
                            name="d", business_name="d", grain="day", applicable_scenario="",
                            forbidden_scenario="", physical_table="t", is_published=True,
                            metrics=(m1,), fields=fields,
                        ))
        rev2 = Revision(id=2, dataset_name="d", parent_id=1, state=RevisionState.DRAFT,
                        dataset_definition=DatasetDef(
                            name="d", business_name="d", grain="day", applicable_scenario="",
                            forbidden_scenario="", physical_table="t", is_published=False,
                            metrics=(m2,), fields=fields,
                        ))

        diff = diff_revisions(rev1, rev2)

        # Modified metric
        modified = [d for d in diff if d.kind == "modified" and "metrics" in d.path]
        assert len(modified) == 1
        assert modified[0].before["time_field"] == "t1"
        assert modified[0].after["time_field"] == "t2"

    def test_diff_different_datasets_raises(self):
        """Cannot diff revisions of different datasets."""
        from app.semantic.revision import diff_revisions, RevisionError

        rev1 = _make_revision(state="published", dataset_name="orders")
        rev2 = _make_revision(state="draft", dataset_name="users", id=2)

        with pytest.raises(RevisionError):
            diff_revisions(rev1, rev2)


class TestBreakingChanges:
    """Test breaking change detection."""

    def test_removed_metric_is_breaking(self):
        """Removing a metric is a breaking change."""
        from app.semantic.revision import diff_revisions, has_breaking_changes

        rev1 = _make_revision(state="published", metric_count=2, id=1)
        rev2 = _make_revision(state="draft", metric_count=1, id=2)

        diff = diff_revisions(rev1, rev2)

        assert has_breaking_changes(diff) is True

    def test_modified_time_field_is_breaking(self):
        """Modifying a metric's time_field is breaking."""
        from app.semantic.model import DatasetDef, FieldDef, MetricDef
        from app.semantic.revision import (
            diff_revisions, has_breaking_changes, Revision, RevisionState,
        )

        fields = (FieldDef(name="amount", business_name="a", physical_column="amount", semantic_type="measure"),)
        m1 = MetricDef(name="m", business_name="m", description="",
                       kind="atomic", time_field="t1", source_field="amount", aggregation="sum")
        m2 = MetricDef(name="m", business_name="m", description="",
                       kind="atomic", time_field="t2",  # Changed
                       source_field="amount", aggregation="sum")

        rev1 = Revision(id=1, dataset_name="d", parent_id=None, state=RevisionState.PUBLISHED,
                        dataset_definition=DatasetDef(name="d", business_name="d", grain="day",
                                                     applicable_scenario="", forbidden_scenario="",
                                                     physical_table="t", is_published=True,
                                                     metrics=(m1,), fields=fields))
        rev2 = Revision(id=2, dataset_name="d", parent_id=1, state=RevisionState.DRAFT,
                        dataset_definition=DatasetDef(name="d", business_name="d", grain="day",
                                                     applicable_scenario="", forbidden_scenario="",
                                                     physical_table="t", is_published=False,
                                                     metrics=(m2,), fields=fields))

        diff = diff_revisions(rev1, rev2)

        assert has_breaking_changes(diff) is True

    def test_added_field_is_not_breaking(self):
        """Adding a new field is not breaking."""
        from app.semantic.revision import diff_revisions, has_breaking_changes

        rev1 = _make_revision(state="published", field_count=1, id=1)
        rev2 = _make_revision(state="draft", field_count=2, id=2)

        diff = diff_revisions(rev1, rev2)

        assert has_breaking_changes(diff) is False

    def test_added_metric_is_not_breaking(self):
        """Adding a new metric is not breaking."""
        from app.semantic.revision import diff_revisions, has_breaking_changes

        rev1 = _make_revision(state="published", metric_count=1, id=1)
        rev2 = _make_revision(state="draft", metric_count=2, id=2)

        diff = diff_revisions(rev1, rev2)

        assert has_breaking_changes(diff) is False


class TestCloneForDraft:
    """Test cloning a revision for a new draft."""

    def test_clone_creates_draft(self):
        """clone_for_draft creates a new draft revision."""
        from app.semantic.revision import (
            Revision, RevisionState, clone_for_draft,
        )

        parent = _make_revision(state="published", id=1)
        new_def = _make_dataset(field_count=3)
        draft = clone_for_draft(parent, new_id=2, new_definition=new_def)

        assert draft.state == RevisionState.DRAFT
        assert draft.id == 2
        assert draft.parent_id == 1

    def test_clone_carries_new_definition(self):
        """Clone carries the new (potentially modified) definition."""
        from app.semantic.revision import clone_for_draft

        parent = _make_revision(state="published", id=1, field_count=2)
        new_def = _make_dataset(field_count=3)
        draft = clone_for_draft(parent, new_id=2, new_definition=new_def)

        # New definition has 3 fields
        assert len(draft.dataset_definition.fields) == 3

    def test_clone_preserves_parent_lineage(self):
        """Clone references parent for diff and history."""
        from app.semantic.revision import clone_for_draft

        parent = _make_revision(state="published", id=42)
        draft = clone_for_draft(parent, new_id=43, new_definition=_make_dataset())

        assert draft.parent_id == 42


class TestRevisionLifecycle:
    """Test the full revision lifecycle end-to-end."""

    def test_full_lifecycle(self):
        """Revision goes through full state machine."""
        from app.semantic.revision import RevisionState, validate_transition

        rev = _make_revision(state="draft")
        assert rev.state == RevisionState.DRAFT

        # draft -> linted
        validate_transition(rev.state, RevisionState.LINTED)
        rev = rev.with_state(RevisionState.LINTED)
        assert rev.state == RevisionState.LINTED

        # linted -> approved
        validate_transition(rev.state, RevisionState.APPROVED)
        rev = rev.mark_approved("alice")
        rev = rev.with_state(RevisionState.APPROVED)
        assert rev.state == RevisionState.APPROVED
        assert rev.approved_by == "alice"

        # approved -> published
        validate_transition(rev.state, RevisionState.PUBLISHED)
        rev = rev.with_state(RevisionState.PUBLISHED)
        assert rev.state == RevisionState.PUBLISHED
        assert rev.published_at is not None

        # published -> retired (superseded)
        validate_transition(rev.state, RevisionState.RETIRED)
        rev = rev.with_state(RevisionState.RETIRED)
        assert rev.state == RevisionState.RETIRED
        assert rev.retired_at is not None

    def test_rollback_from_retired(self):
        """Retired revision can be rolled back to published."""
        from app.semantic.revision import RevisionState, validate_transition

        retired = _make_revision(state="retired", id=1)

        # Rollback: retired -> published
        validate_transition(retired.state, RevisionState.PUBLISHED)
        rolled_back = retired.with_state(RevisionState.PUBLISHED)

        assert rolled_back.state == RevisionState.PUBLISHED


class TestLintReport:
    """Test LintReport functionality."""

    def test_lint_report_passes(self):
        """LintReport(passed=True) constructs."""
        from app.semantic.revision import LintReport

        report = LintReport(passed=True)
        assert report.passed is True
        assert report.issues == ()

    def test_lint_report_with_issues(self):
        """LintReport can carry issues."""
        from app.semantic.revision import LintReport

        report = LintReport(
            passed=False,
            issues=("alias conflict: '东方' shared by 2 values",),
            warnings=("metric 'foo' has no description",),
        )

        assert report.passed is False
        assert len(report.issues) == 1
        assert len(report.warnings) == 1
