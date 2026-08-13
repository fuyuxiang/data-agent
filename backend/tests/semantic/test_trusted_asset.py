"""Tests for Trusted Asset governance (S4 P2-04)."""

import pytest
from datetime import datetime, timedelta


pytestmark = pytest.mark.no_db


def _make_asset(
    *,
    id: int = 1,
    name: str = "East sales this month",
    triggers: tuple[str, ...] = ("本月华东销售额",),
    parameters: tuple = (),
    revision_id: int = 1,
    verified_by: str = "alice",
    expires_at: datetime | None = None,
    is_active: bool = True,
    review_status: str = "approved",
    last_validation_result: str = "passed",
    consecutive_failures: int = 0,
    usage_count: int = 0,
    failure_count: int = 0,
    eval_excluded: bool = False,
):
    """Build a sample trusted asset for testing."""
    from app.semantic.trusted_asset import (
        TrustedAsset, ReviewStatus, ValidationResult, ParameterSpec,
    )

    return TrustedAsset(
        id=id,
        name=name,
        domain="sales",
        trigger_questions=triggers,
        canonical_plan_template=None,  # Not under test here
        parameter_schema=parameters,
        semantic_revision_id=revision_id,
        verified_by=verified_by,
        verified_at=datetime(2026, 1, 1),
        review_status=ReviewStatus(review_status),
        expires_at=expires_at,
        is_active=is_active,
        last_validation_result=ValidationResult(last_validation_result),
        usage_count=usage_count,
        failure_count=failure_count,
        consecutive_failures=consecutive_failures,
        eval_excluded=eval_excluded,
    )


def _make_param(name: str, ptype: str = "string", required: bool = True, values: tuple = ()):
    """Build a sample parameter spec."""
    from app.semantic.trusted_asset import ParameterSpec

    return ParameterSpec(
        name=name, param_type=ptype, required=required, enum_values=values
    )


class TestTrustedAssetBasics:
    """Test TrustedAsset construction and basic access."""

    def test_asset_constructs(self):
        """Asset with required fields constructs."""
        asset = _make_asset()
        assert asset.id == 1
        assert asset.name == "East sales this month"

    def test_asset_default_active(self):
        """Asset is active by default."""
        asset = _make_asset()
        assert asset.is_active is True

    def test_asset_default_approved(self):
        """Asset is approved by default."""
        asset = _make_asset()
        assert asset.review_status.value == "approved"

    def test_asset_eval_excluded_default_false(self):
        """Asset is not eval-excluded by default."""
        asset = _make_asset()
        assert asset.eval_excluded is False


class TestExpiry:
    """Test asset expiry logic."""

    def test_no_expiry_not_expired(self):
        """Asset without expires_at is never expired."""
        asset = _make_asset(expires_at=None)
        assert asset.is_expired() is False

    def test_future_expiry_not_expired(self):
        """Future expires_at is not yet expired."""
        future = datetime(2099, 1, 1)
        asset = _make_asset(expires_at=future)
        assert asset.is_expired() is False

    def test_past_expiry_expired(self):
        """Past expires_at is expired."""
        past = datetime(2020, 1, 1)
        asset = _make_asset(expires_at=past)
        assert asset.is_expired() is True

    def test_recallable_when_expired(self):
        """Expired assets are not recallable."""
        past = datetime(2020, 1, 1)
        asset = _make_asset(expires_at=past)
        assert asset.is_recallable() is False

    def test_recallable_when_not_expired(self):
        """Non-expired active assets are recallable."""
        future = datetime(2099, 1, 1)
        asset = _make_asset(expires_at=future)
        assert asset.is_recallable() is True


class TestRecallableConditions:
    """Test is_recallable() conditions."""

    def test_inactive_not_recallable(self):
        """Inactive assets are not recallable."""
        asset = _make_asset(is_active=False)
        assert asset.is_recallable() is False

    def test_pending_review_not_recallable(self):
        """Pending-review assets are not recallable."""
        asset = _make_asset(review_status="pending")
        assert asset.is_recallable() is False

    def test_retired_not_recallable(self):
        """Retired assets are not recallable."""
        asset = _make_asset(review_status="retired")
        assert asset.is_recallable() is False

    def test_failed_validation_not_recallable(self):
        """Assets with failed last validation are not recallable."""
        asset = _make_asset(last_validation_result="failed")
        assert asset.is_recallable() is False

    def test_pending_validation_is_recallable(self):
        """Pending validation (never run) is still recallable."""
        asset = _make_asset(last_validation_result="pending")
        assert asset.is_recallable() is True


class TestRecallTracking:
    """Test usage_count and failure tracking."""

    def test_with_recall_increments_count(self):
        """Successful recall increments usage_count."""
        asset = _make_asset(usage_count=5)
        recalled = asset.with_recall()
        assert recalled.usage_count == 6

    def test_with_recall_resets_consecutive_failures(self):
        """Successful recall resets consecutive_failures."""
        asset = _make_asset(consecutive_failures=2)
        recalled = asset.with_recall()
        assert recalled.consecutive_failures == 0

    def test_with_failure_increments_count(self):
        """Failure increments failure_count."""
        asset = _make_asset(failure_count=3)
        failed = asset.with_failure()
        assert failed.failure_count == 4

    def test_with_failure_increments_consecutive(self):
        """Failure increments consecutive_failures."""
        asset = _make_asset(consecutive_failures=2)
        failed = asset.with_failure()
        assert failed.consecutive_failures == 3

    def test_failure_threshold_deactivates(self):
        """3 consecutive failures deactivate the asset."""
        # Start with 2 consecutive failures
        asset = _make_asset(consecutive_failures=2)
        failed = asset.with_failure()
        # Now at 3, which is the default threshold; should deactivate
        assert failed.is_active is False

    def test_failure_below_threshold_keeps_active(self):
        """Below threshold, asset stays active."""
        asset = _make_asset(consecutive_failures=0)
        failed = asset.with_failure()
        # 1 failure, still active
        assert failed.is_active is True


class TestParameterBinding:
    """Test parameter binding and validation."""

    def test_required_param_missing(self):
        """Missing required parameter returns error."""
        asset = _make_asset(parameters=(_make_param("region"),))
        errors = bind_params_for_test(asset, {})
        assert any("region" in e for e in errors)

    def test_required_param_present(self):
        """Present required parameter passes."""
        asset = _make_asset(parameters=(_make_param("region"),))
        errors = bind_params_for_test(asset, {"region": "east"})
        assert errors == ()

    def test_optional_param_can_be_omitted(self):
        """Optional parameter can be omitted."""
        asset = _make_asset(parameters=(_make_param("limit", required=False),))
        errors = bind_params_for_test(asset, {})
        assert errors == ()

    def test_enum_value_validated(self):
        """Enum parameter value must be in enum_values."""
        asset = _make_asset(
            parameters=(_make_param("region", ptype="enum", values=("east", "west")),)
        )
        errors = bind_params_for_test(asset, {"region": "north"})
        assert any("north" in e and "region" in e for e in errors)

    def test_enum_value_accepted(self):
        """Valid enum value passes."""
        asset = _make_asset(
            parameters=(_make_param("region", ptype="enum", values=("east", "west")),)
        )
        errors = bind_params_for_test(asset, {"region": "east"})
        assert errors == ()

    def test_unknown_parameter_rejected(self):
        """Unknown parameters are rejected."""
        asset = _make_asset(parameters=(_make_param("region"),))
        errors = bind_params_for_test(asset, {"region": "east", "extra": "x"})
        assert any("Unknown" in e for e in errors)


def bind_params_for_test(asset, params):
    """Helper to call bind_parameters without leaking the import."""
    from app.semantic.trusted_asset import bind_parameters
    return bind_parameters(asset, params)


class TestRecallEngine:
    """Test the recall engine."""

    def test_exact_text_match(self):
        """Exact trigger text match returns high-confidence match."""
        from app.semantic.trusted_asset import RecallQuery, recall

        asset = _make_asset(triggers=("本月华东销售额",))
        query = RecallQuery(
            question="本月华东销售额",
            semantic_revision_id=1,
            principal_id=100,
        )
        result = recall(query, (asset,))

        assert len(result) == 1
        assert result[0].match_reason == "exact_text"
        assert result[0].confidence > 0.9

    def test_exact_text_match_case_insensitive(self):
        """Match is case-insensitive after casefold."""
        from app.semantic.trusted_asset import RecallQuery, recall

        asset = _make_asset(triggers=("本月华东销售额",))
        # Note: "本月华东销售额" doesn't change with casefold, but test
        # the function handles whitespace
        query = RecallQuery(
            question="  本月华东销售额  ",
            semantic_revision_id=1,
            principal_id=100,
        )
        result = recall(query, (asset,))
        assert len(result) == 1

    def test_no_match_returns_empty(self):
        """No matching trigger returns empty."""
        from app.semantic.trusted_asset import RecallQuery, recall

        asset = _make_asset(triggers=("different trigger",))
        query = RecallQuery(
            question="本月华东销售额",
            semantic_revision_id=1,
            principal_id=100,
        )
        result = recall(query, (asset,))
        assert result == ()

    def test_revision_mismatch_excludes_asset(self):
        """Asset with different revision is not recalled."""
        from app.semantic.trusted_asset import RecallQuery, recall

        asset = _make_asset(triggers=("本月华东销售额",), revision_id=2)
        query = RecallQuery(
            question="本月华东销售额",
            semantic_revision_id=1,  # Different from asset
            principal_id=100,
        )
        result = recall(query, (asset,))
        assert result == ()

    def test_inactive_asset_not_recalled(self):
        """Inactive assets are not recalled."""
        from app.semantic.trusted_asset import RecallQuery, recall

        asset = _make_asset(triggers=("本月华东销售额",), is_active=False)
        query = RecallQuery(
            question="本月华东销售额",
            semantic_revision_id=1,
            principal_id=100,
        )
        result = recall(query, (asset,))
        assert result == ()

    def test_expired_asset_not_recalled(self):
        """Expired assets are not recalled."""
        from app.semantic.trusted_asset import RecallQuery, recall

        past = datetime(2020, 1, 1)
        asset = _make_asset(triggers=("q",), expires_at=past)
        query = RecallQuery(
            question="q", semantic_revision_id=1, principal_id=100
        )
        result = recall(query, (asset,))
        assert result == ()


class TestParametricRecall:
    """Test parametric recall (shared asset for similar questions)."""

    def test_parametric_asset_with_region(self):
        """One asset covers multiple regions via parameter."""
        from app.semantic.trusted_asset import RecallQuery, recall

        # Single asset with region parameter
        asset = _make_asset(
            name="Region sales this month",
            triggers=("本月销售额",),  # Generic trigger
            parameters=(_make_param("region", ptype="enum", values=("east", "west", "south")),),
        )

        # East and West both share the same asset
        for region in ("east", "west", "south"):
            query = RecallQuery(
                question="本月销售额",  # Generic
                semantic_revision_id=1,
                principal_id=100,
                parameter_hints={"region": region},
            )
            result = recall(query, (asset,))
            assert len(result) == 1
            assert result[0].parameters["region"] == region

    def test_parametric_asset_validates_param_value(self):
        """Asset with enum param rejects invalid values via bind_parameters."""
        asset = _make_asset(
            parameters=(_make_param("region", ptype="enum", values=("east", "west")),)
        )

        # Invalid value: north is not in enum
        errors = bind_params_for_test(asset, {"region": "north"})
        assert any("north" in e for e in errors)


class TestEvalExclusion:
    """Test eval_excluded flag (S5 isolation)."""

    def test_eval_excluded_flag(self):
        """eval_excluded flag is settable."""
        asset = _make_asset(eval_excluded=True)
        assert asset.eval_excluded is True

    def test_recallable_still_works_for_eval_excluded(self):
        """is_recallable does not check eval_excluded (caller's concern)."""
        # The recall engine itself does not exclude eval_excluded assets;
        # the S5 eval framework does, by not including them in the asset pool.
        asset = _make_asset(eval_excluded=True)
        assert asset.is_recallable() is True
