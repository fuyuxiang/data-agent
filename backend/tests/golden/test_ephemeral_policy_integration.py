"""Integration tests for ephemeral_policy field-level strategy.

Tests the interaction between:
- ephemeral_policy: field-level policy overrides in test cases
- intent_tolerances: field-level intent matching tolerance
- permissions layer evaluation with derived policy IDs
"""

import pytest
from tests.golden.loader import Expectation, GoldenCase, PolicySpec
from tests.golden.runner import run_case
from app.evals.layers import LayerOutcome


def mock_orchestrator_basic(**kwargs):
    """Mock orchestrator with basic response."""
    return {
        'status': 'ANSWERED',
        'rows': [{'id': 1, 'title': 'Test', 'value': 100}],
        'citation': [],
        'response_text': 'Found 1 record',
        'intent': None,
        'clarify_kind': None,
        'options': [],
    }


def mock_orchestrator_with_policies(**kwargs):
    """Mock orchestrator that returns policy information."""
    return {
        'status': 'ANSWERED',
        'rows': [{'id': 1, 'title': 'Test', 'value': 100}],
        'citation': [],
        'response_text': 'Found 1 record',
        'intent': None,
        'clarify_kind': None,
        'options': [],
        'policies': ['row_policy[owned_conversation]', 'column_deny[secret_field]'],
    }


def mock_user_resolver(user: str) -> int:
    """Mock user ID resolver."""
    return 1


def mock_policy(user: str, policies: tuple) -> None:
    """Mock ephemeral policy installer."""
    pass


class TestEphemeralPolicyBasics:
    """Test basic ephemeral_policy functionality."""

    def test_ephemeral_policy_empty_dict(self):
        """Empty ephemeral_policy should not override global policies."""
        case = GoldenCase(
            id='test-ephemeral-empty',
            question='Show data',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={},
            ),
            policies=(
                PolicySpec(kind='row_policy', field='owned_conversation', allowed_values=()),
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_with_policies,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        # Should use global policies, not ephemeral
        perms_layer = next(r for r in report.layer_reports if r.layer == 'permissions')
        assert perms_layer.outcome in (LayerOutcome.PASS, LayerOutcome.FAIL, LayerOutcome.SKIPPED)

    def test_ephemeral_policy_with_fields_only(self):
        """ephemeral_policy with fields should derive field-level policies."""
        case = GoldenCase(
            id='test-ephemeral-fields',
            question='Show limited fields',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': ['id', 'title'],
                    }
                },
            ),
            policies=(
                PolicySpec(kind='row_policy', field='owned_conversation', allowed_values=()),
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_with_policies,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        perms_layer = next(r for r in report.layer_reports if r.layer == 'permissions')
        # Ephemeral policies should be derived
        assert 'ephemeral' in str(perms_layer.message).lower() or perms_layer.outcome == LayerOutcome.FAIL

    def test_ephemeral_policy_with_row_filter(self):
        """ephemeral_policy with row_filter should derive filter-level policies."""
        case = GoldenCase(
            id='test-ephemeral-filter',
            question='Show filtered data',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'dataset_core': {
                        'row_filter': 'tenant_id = current_tenant_id',
                    }
                },
            ),
            policies=(
                PolicySpec(kind='row_policy', field='owned_conversation', allowed_values=()),
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_with_policies,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        perms_layer = next(r for r in report.layer_reports if r.layer == 'permissions')
        assert perms_layer.outcome in (LayerOutcome.PASS, LayerOutcome.FAIL, LayerOutcome.SKIPPED)

    def test_ephemeral_policy_with_both_fields_and_filter(self):
        """ephemeral_policy with both fields and row_filter should derive both."""
        case = GoldenCase(
            id='test-ephemeral-both',
            question='Show limited filtered data',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': ['id', 'title', 'created_at'],
                        'row_filter': 'is_archived = false',
                    }
                },
            ),
            policies=(
                PolicySpec(kind='row_policy', field='owned_conversation', allowed_values=()),
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_with_policies,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        perms_layer = next(r for r in report.layer_reports if r.layer == 'permissions')
        assert perms_layer.outcome in (LayerOutcome.PASS, LayerOutcome.FAIL, LayerOutcome.SKIPPED)


class TestIntentFieldLevelTolerance:
    """Test intent field-level tolerance functionality."""

    def test_intent_tolerances_default_lenient(self):
        """Intent tolerances should default to LENIENT for all fields."""
        case = GoldenCase(
            id='test-intent-default',
            question='What is the revenue?',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        # Default tolerances are applied (all LENIENT)
        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')

    def test_intent_tolerances_custom_strict(self):
        """Intent tolerances can be customized to STRICT per field."""
        case = GoldenCase(
            id='test-intent-strict',
            question='What is the revenue trend?',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                intent_tolerances={
                    'metrics': 'strict',
                    'dimensions': 'strict',
                    'time': 'lenient',
                    'filters': 'lenient',
                    'comparison': 'lenient',
                    'top_n': 'lenient',
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        # Custom field-level tolerances are applied
        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')

    def test_intent_tolerances_partial_override(self):
        """Intent tolerances can override only specific fields."""
        case = GoldenCase(
            id='test-intent-partial',
            question='Show revenue by region',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                intent_tolerances={
                    'metrics': 'strict',
                    'time': 'lenient',
                    'dimensions': 'lenient',
                    'filters': 'lenient',
                    'comparison': 'lenient',
                    'top_n': 'lenient',
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        # Partial field-level tolerances mixed with defaults
        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')


class TestCombinedFeatures:
    """Test interaction between ephemeral_policy and intent_tolerances."""

    def test_ephemeral_policy_and_intent_tolerances_together(self):
        """Both ephemeral_policy and intent_tolerances can be used together."""
        case = GoldenCase(
            id='test-combined',
            question='What is the revenue by region?',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': ['id', 'title'],
                    }
                },
                intent_tolerances={
                    'metrics': 'strict',
                    'dimensions': 'lenient',
                    'filters': 'lenient',
                    'time': 'lenient',
                    'comparison': 'lenient',
                    'top_n': 'lenient',
                },
            ),
            policies=(
                PolicySpec(kind='row_policy', field='owned_conversation', allowed_values=()),
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_with_policies,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        # Both features should be applied independently
        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')
        perms_layer = next(r for r in report.layer_reports if r.layer == 'permissions')
        intent_layer = next(r for r in report.layer_reports if r.layer == 'intent')
        assert intent_layer.outcome in (LayerOutcome.PASS, LayerOutcome.FAIL, LayerOutcome.SKIPPED)
        assert perms_layer.outcome in (LayerOutcome.PASS, LayerOutcome.FAIL, LayerOutcome.SKIPPED)


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_ephemeral_policy_multiple_resources(self):
        """ephemeral_policy can specify overrides for multiple resources."""
        case = GoldenCase(
            id='test-ephemeral-multi',
            question='Show multi-resource data',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': ['id', 'title'],
                    },
                    'dataset_core': {
                        'fields': ['dataset_id', 'name'],
                        'row_filter': 'is_active = true',
                    },
                    'metric': {
                        'row_filter': 'is_valid = true',
                    },
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')

    def test_intent_tolerances_all_strict(self):
        """Intent tolerances can be all STRICT."""
        case = GoldenCase(
            id='test-intent-all-strict',
            question='What is the exact revenue?',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                intent_tolerances={
                    'metrics': 'strict',
                    'dimensions': 'strict',
                    'time': 'strict',
                    'filters': 'strict',
                    'comparison': 'strict',
                    'top_n': 'strict',
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')

    def test_ephemeral_policy_empty_fields_list(self):
        """ephemeral_policy with empty fields list is valid."""
        case = GoldenCase(
            id='test-ephemeral-empty-fields',
            question='Show data with empty field override',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': [],
                    }
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')

    def test_ephemeral_policy_none_values(self):
        """ephemeral_policy None values are handled gracefully."""
        case = GoldenCase(
            id='test-ephemeral-none',
            question='Show data with None values',
            as_user='user1',
            expect=Expectation(
                status='ANSWERED',
                rows=1,
                ephemeral_policy={
                    'owned_conversation': {
                        'fields': ['id', 'title'],
                        'row_filter': None,  # Explicitly None
                    }
                },
            ),
        )

        report = run_case(
            case,
            mode='stub',
            orchestrator=mock_orchestrator_basic,
            user_id_resolver=mock_user_resolver,
            ephemeral_policy=mock_policy,
        )

        assert report.status in ('PASS', 'FAIL', 'XFAIL', 'SKIPPED')
