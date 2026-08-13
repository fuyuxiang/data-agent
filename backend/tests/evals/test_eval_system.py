"""Tests for S5 evaluation system rebuild."""

import pytest


pytestmark = pytest.mark.no_db


class TestLayerOutcomes:
    """Test layer outcome enum."""

    def test_pass_outcome(self):
        """PASS outcome value."""
        from app.evals.layers import LayerOutcome
        assert LayerOutcome.PASS.value == "pass"

    def test_fail_outcome(self):
        """FAIL outcome value."""
        from app.evals.layers import LayerOutcome
        assert LayerOutcome.FAIL.value == "fail"

    def test_skipped_outcome(self):
        """SKIPPED outcome value (case doesn't fail, but is reported)."""
        from app.evals.layers import LayerOutcome
        assert LayerOutcome.SKIPPED.value == "skipped"


class TestFieldCategories:
    """Test strict vs lenient field categories."""

    def test_strict_fields_includes_metrics(self):
        """metrics is strict (must FAIL on diff)."""
        from app.evals.layers import STRICT_FIELDS
        assert "metrics" in STRICT_FIELDS

    def test_strict_fields_includes_time(self):
        """time is strict."""
        from app.evals.layers import STRICT_FIELDS
        assert "time" in STRICT_FIELDS

    def test_strict_fields_includes_permissions(self):
        """permissions is strict."""
        from app.evals.layers import STRICT_FIELDS
        assert "permissions" in STRICT_FIELDS

    def test_lenient_fields_includes_dimensions(self):
        """dimensions is lenient (set-equality)."""
        from app.evals.layers import LENIENT_FIELDS
        assert "dimensions" in LENIENT_FIELDS

    def test_lenient_fields_includes_filters(self):
        """filters is lenient."""
        from app.evals.layers import LENIENT_FIELDS
        assert "filters" in LENIENT_FIELDS


class TestIntentLayer:
    """Test intent layer evaluation."""

    def test_identical_intents_pass(self):
        """Identical intents pass."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        intent = {"metrics": ["sales"], "dimensions": ["region"]}
        report = evaluate_intent_layer(intent, intent)

        assert report.outcome == LayerOutcome.PASS

    def test_strict_field_diff_fails(self):
        """Strict field (metrics) diff fails the case."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        expected = {"metrics": ["sales"], "dimensions": ["region"]}
        actual = {"metrics": ["refund"], "dimensions": ["region"]}
        report = evaluate_intent_layer(expected, actual)

        assert report.outcome == LayerOutcome.FAIL
        assert any(d.field == "metrics" for d in report.diffs)

    def test_lenient_field_set_equality_passes(self):
        """Lenient field passes when sets are equal but order differs."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        expected = {"metrics": ["sales"], "dimensions": ["region", "product"]}
        actual = {"metrics": ["sales"], "dimensions": ["product", "region"]}
        report = evaluate_intent_layer(expected, actual)

        assert report.outcome == LayerOutcome.PASS

    def test_lenient_field_different_sets_fails(self):
        """Lenient field fails when sets differ."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        expected = {"metrics": ["sales"], "dimensions": ["region"]}
        actual = {"metrics": ["sales"], "dimensions": ["product"]}
        report = evaluate_intent_layer(expected, actual)

        assert report.outcome == LayerOutcome.FAIL

    def test_both_none_skipped(self):
        """Both None -> SKIPPED (not a failure)."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        report = evaluate_intent_layer(None, None)

        assert report.outcome == LayerOutcome.SKIPPED

    def test_one_none_fails(self):
        """One None and one not -> FAIL."""
        from app.evals.layers import evaluate_intent_layer, LayerOutcome

        report = evaluate_intent_layer({"metrics": ["x"]}, None)
        assert report.outcome == LayerOutcome.FAIL


class TestStatusLayer:
    """Test status layer evaluation."""

    def test_same_status_passes(self):
        """Same status passes."""
        from app.evals.layers import evaluate_status_layer, LayerOutcome

        report = evaluate_status_layer("answered", "answered")
        assert report.outcome == LayerOutcome.PASS

    def test_different_status_fails(self):
        """Different status fails (status is strict)."""
        from app.evals.layers import evaluate_status_layer, LayerOutcome

        report = evaluate_status_layer("answered", "failed")
        assert report.outcome == LayerOutcome.FAIL


class TestSQLLayer:
    """Test SQL layer evaluation."""

    def test_same_sql_passes(self):
        """Same SQL passes."""
        from app.evals.layers import evaluate_sql_layer, LayerOutcome

        sql = "SELECT 1"
        report = evaluate_sql_layer(sql, sql)
        assert report.outcome == LayerOutcome.PASS

    def test_different_sql_fails(self):
        """Different SQL fails."""
        from app.evals.layers import evaluate_sql_layer, LayerOutcome

        report = evaluate_sql_layer("SELECT 1", "SELECT 2")
        assert report.outcome == LayerOutcome.FAIL

    def test_whitespace_insensitive(self):
        """Whitespace is stripped before comparison."""
        from app.evals.layers import evaluate_sql_layer, LayerOutcome

        report = evaluate_sql_layer("SELECT 1", "  SELECT 1  ")
        assert report.outcome == LayerOutcome.PASS


class TestResultLayer:
    """Test result row comparison."""

    def test_same_rows_pass(self):
        """Identical result rows pass."""
        from app.evals.layers import evaluate_result_layer, LayerOutcome

        rows = [("east", 100), ("west", 200)]
        report = evaluate_result_layer(rows, rows)
        assert report.outcome == LayerOutcome.PASS

    def test_different_rows_fail(self):
        """Different rows fail."""
        from app.evals.layers import evaluate_result_layer, LayerOutcome

        report = evaluate_result_layer([("east", 100)], [("east", 200)])
        assert report.outcome == LayerOutcome.FAIL


class TestTraceLayer:
    """Test trace stage comparison."""

    def test_same_stages_pass(self):
        """Same stage sequence passes."""
        from app.evals.layers import evaluate_trace_layer, LayerOutcome

        stages = ["intent", "compile", "execute"]
        report = evaluate_trace_layer(stages, stages)
        assert report.outcome == LayerOutcome.PASS

    def test_different_stages_fails(self):
        """Different stage sequences fail."""
        from app.evals.layers import evaluate_trace_layer, LayerOutcome

        report = evaluate_trace_layer(["intent"], ["intent", "compile"])
        assert report.outcome == LayerOutcome.FAIL


class TestPermissionsLayer:
    """Test permissions layer."""

    def test_same_policies_pass(self):
        """Same row policies pass."""
        from app.evals.layers import evaluate_permissions_layer, LayerOutcome

        policies = ["region=east", "tier=gold"]
        report = evaluate_permissions_layer(policies, policies)
        assert report.outcome == LayerOutcome.PASS

    def test_policy_order_doesnt_matter(self):
        """Policy order doesn't matter (set equality)."""
        from app.evals.layers import evaluate_permissions_layer, LayerOutcome

        report = evaluate_permissions_layer(
            ["a", "b"], ["b", "a"]
        )
        assert report.outcome == LayerOutcome.PASS

    def test_different_policies_fail(self):
        """Different policies fail."""
        from app.evals.layers import evaluate_permissions_layer, LayerOutcome

        report = evaluate_permissions_layer(["a"], ["b"])
        assert report.outcome == LayerOutcome.FAIL


class TestNonfunctionalLayer:
    """Test non-functional layer (latency)."""

    def test_low_latency_passes(self):
        """Latency under threshold passes."""
        from app.evals.layers import evaluate_nonfunctional_layer, LayerOutcome

        report = evaluate_nonfunctional_layer(latency_ms=1000, token_usage=500)
        assert report.outcome == LayerOutcome.PASS

    def test_high_latency_fails(self):
        """Latency over threshold fails."""
        from app.evals.layers import evaluate_nonfunctional_layer, LayerOutcome

        report = evaluate_nonfunctional_layer(
            latency_ms=60000, token_usage=500, threshold=30000
        )
        assert report.outcome == LayerOutcome.FAIL


class TestCasePasses:
    """Test case_passes aggregate."""

    def test_all_pass_means_case_passes(self):
        """All layers PASS means case passes."""
        from app.evals.layers import (
            LayerReport, LayerOutcome, case_passes,
        )

        reports = (
            LayerReport(layer="intent", outcome=LayerOutcome.PASS),
            LayerReport(layer="status", outcome=LayerOutcome.PASS),
        )

        assert case_passes(reports) is True

    def test_any_fail_means_case_fails(self):
        """Any layer FAIL means case fails."""
        from app.evals.layers import (
            LayerReport, LayerOutcome, case_passes,
        )

        reports = (
            LayerReport(layer="intent", outcome=LayerOutcome.PASS),
            LayerReport(layer="status", outcome=LayerOutcome.FAIL),
        )

        assert case_passes(reports) is False

    def test_skipped_does_not_fail(self):
        """SKIPPED layers don't fail the case."""
        from app.evals.layers import (
            LayerReport, LayerOutcome, case_passes,
        )

        reports = (
            LayerReport(layer="intent", outcome=LayerOutcome.PASS),
            LayerReport(layer="trace", outcome=LayerOutcome.SKIPPED),
        )

        assert case_passes(reports) is True


class TestEvalRun:
    """Test EvalRun lifecycle and gating."""

    def test_eval_run_constructs(self):
        """EvalRun with required fields constructs."""
        from app.evals.run import EvalRun
        from datetime import datetime

        run = EvalRun(
            id="run-001",
            name="smoke",
            started_at=datetime(2026, 8, 13),
            completed_at=None,
            semantic_revision_id=1,
            prompt_version="v1",
            model_snapshot="gpt-4o-2024",
        )

        assert run.id == "run-001"

    def test_run_hash_is_deterministic(self):
        """Same configuration produces same hash."""
        from app.evals.run import EvalRun
        from datetime import datetime

        common = dict(
            name="smoke",
            started_at=datetime(2026, 8, 13),
            completed_at=None,
            semantic_revision_id=1,
            prompt_version="v1",
            model_snapshot="gpt-4o-2024",
        )
        r1 = EvalRun(id="a", **common)
        r2 = EvalRun(id="b", **common)
        assert r1.hash() == r2.hash()

    def test_run_hash_changes_with_revision(self):
        """Hash changes when semantic revision changes."""
        from app.evals.run import EvalRun
        from datetime import datetime

        common = dict(
            name="smoke",
            started_at=datetime(2026, 8, 13),
            completed_at=None,
            prompt_version="v1",
            model_snapshot="gpt-4o-2024",
        )
        r1 = EvalRun(id="a", semantic_revision_id=1, **common)
        r2 = EvalRun(id="a", semantic_revision_id=2, **common)
        assert r1.hash() != r2.hash()


class TestGateDecision:
    """Test EvalRun gate decision (regression vs threshold)."""

    def test_passes_when_no_regression_above_threshold(self):
        """No regression + above threshold -> PASS."""
        from app.evals.run import EvalRun, CaseOutcome, GateDecision
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        outcomes = tuple(
            CaseOutcome(
                case_id=f"c{i}",
                case_name=f"case {i}",
                layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
                case_passed=True,
            )
            for i in range(10)
        )
        run = EvalRun(
            id="r1", name="r", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=outcomes, threshold=0.95,
        )
        assert run.gate_decision() == GateDecision.PASS

    def test_fails_below_threshold(self):
        """Below threshold -> FAIL_BELOW_THRESHOLD."""
        from app.evals.run import EvalRun, CaseOutcome, GateDecision
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        # 5 pass, 5 fail -> 50% pass, below 95% threshold
        outcomes = tuple(
            CaseOutcome(
                case_id=f"c{i}",
                case_name=f"case {i}",
                layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS if i < 5 else LayerOutcome.FAIL),),
                case_passed=i < 5,
            )
            for i in range(10)
        )
        run = EvalRun(
            id="r1", name="r", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=outcomes, threshold=0.95,
        )
        assert run.gate_decision() == GateDecision.FAIL_BELOW_THRESHOLD

    def test_regression_fails_even_above_threshold(self):
        """Regression (vs baseline) fails first, even above threshold."""
        from app.evals.run import EvalRun, CaseOutcome, GateDecision
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        # Current: 10/10 pass
        current_outcomes = tuple(
            CaseOutcome(
                case_id=f"c{i}",
                case_name=f"case {i}",
                layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
                case_passed=True,
            )
            for i in range(10)
        )
        current = EvalRun(
            id="current", name="r", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=current_outcomes, threshold=0.5,
        )

        # Baseline: c5 is PASS, current claims it's FAIL -> regression
        baseline_outcomes = []
        for i in range(10):
            baseline_outcome = LayerOutcome.PASS
            baseline_outcomes.append(CaseOutcome(
                case_id=f"c{i}",
                case_name=f"case {i}",
                layer_reports=(LayerReport(layer="intent", outcome=baseline_outcome),),
                case_passed=True,
            ))
        baseline = EvalRun(
            id="baseline", name="r", started_at=datetime(2026, 8, 12),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=tuple(baseline_outcomes), threshold=0.5,
        )

        # Current: c5 became FAIL (regression!)
        current_outcomes = []
        for i in range(10):
            current_outcome = LayerOutcome.FAIL if i == 5 else LayerOutcome.PASS
            current_outcomes.append(CaseOutcome(
                case_id=f"c{i}",
                case_name=f"case {i}",
                layer_reports=(LayerReport(layer="intent", outcome=current_outcome),),
                case_passed=(current_outcome == LayerOutcome.PASS),
            ))
        current = EvalRun(
            id="current", name="r", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=tuple(current_outcomes), threshold=0.5,
        )
        baseline = EvalRun(
            id="baseline", name="r", started_at=datetime(2026, 8, 12),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=tuple(baseline_outcomes), threshold=0.5,
        )

        # Threshold is 50%, current passes it. But regression in c5
        # should make the gate FAIL.
        assert current.gate_decision(baseline) == GateDecision.FAIL_REGRESSION


class TestCompareToBaseline:
    """Test baseline comparison."""

    def test_no_regression_on_identical_runs(self):
        """Identical runs have no regression."""
        from app.evals.run import EvalRun, CaseOutcome, compare_to_baseline
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        outcomes = (CaseOutcome(
            case_id="c1",
            case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
            case_passed=True,
        ),)
        run = EvalRun(
            id="r", name="r", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=outcomes,
        )
        baseline = EvalRun(
            id="b", name="b", started_at=datetime(2026, 8, 12),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=outcomes,
        )

        comparisons = compare_to_baseline(run, baseline)
        assert len(comparisons) == 1
        assert comparisons[0].regressed is False

    def test_regression_detected(self):
        """Pass -> Fail is a regression."""
        from app.evals.run import EvalRun, CaseOutcome, compare_to_baseline
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        current_outcomes = (CaseOutcome(
            case_id="c1", case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.FAIL),),
            case_passed=False,
        ),)
        baseline_outcomes = (CaseOutcome(
            case_id="c1", case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
            case_passed=True,
        ),)

        current = EvalRun(
            id="c", name="c", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=current_outcomes,
        )
        baseline = EvalRun(
            id="b", name="b", started_at=datetime(2026, 8, 12),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=baseline_outcomes,
        )

        comparisons = compare_to_baseline(current, baseline)
        assert comparisons[0].regressed is True

    def test_recovery_not_regression(self):
        """Fail -> Pass is recovery, not regression."""
        from app.evals.run import EvalRun, CaseOutcome, compare_to_baseline
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        current_outcomes = (CaseOutcome(
            case_id="c1", case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
            case_passed=True,
        ),)
        baseline_outcomes = (CaseOutcome(
            case_id="c1", case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.FAIL),),
            case_passed=False,
        ),)

        current = EvalRun(
            id="c", name="c", started_at=datetime(2026, 8, 13),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=current_outcomes,
        )
        baseline = EvalRun(
            id="b", name="b", started_at=datetime(2026, 8, 12),
            completed_at=None, semantic_revision_id=1,
            prompt_version="v1", model_snapshot="m",
            case_outcomes=baseline_outcomes,
        )

        comparisons = compare_to_baseline(current, baseline)
        assert comparisons[0].regressed is False


class TestArchivePersistence:
    """Test Eval Run archive on disk."""

    def test_archive_round_trip(self, tmp_path):
        """Write to disk and read back."""
        from app.evals.run import (
            EvalRun, CaseOutcome, archive_run, load_run,
        )
        from app.evals.layers import LayerReport, LayerOutcome
        from datetime import datetime

        outcomes = (CaseOutcome(
            case_id="c1", case_name="c",
            layer_reports=(LayerReport(layer="intent", outcome=LayerOutcome.PASS),),
            case_passed=True,
        ),)
        original = EvalRun(
            id="run-1", name="smoke", started_at=datetime(2026, 8, 13, 10, 0),
            completed_at=datetime(2026, 8, 13, 10, 5),
            semantic_revision_id=1, prompt_version="v1",
            model_snapshot="gpt-4o", case_outcomes=outcomes,
            threshold=0.95, notes="test run",
        )

        path = archive_run(original, tmp_path)
        assert path.exists()

        loaded = load_run(path)
        assert loaded.id == "run-1"
        assert loaded.notes == "test run"
        assert loaded.case_outcomes[0].case_passed is True
        assert loaded.hash() == original.hash()
