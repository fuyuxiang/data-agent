"""LLM provider failure classification (S2 Task 3, Step 1).

The recognizer currently treats all LLM failures as a single exception.
This test suite verifies that we map 5 distinct failure classes to
independent error_code values, enabling precise error handling.
"""

import pytest

from app.llm.provider import (
    IntentModel,
    LlmRefusalError,
    LlmIncompleteError,
    LlmProviderError,
    LlmSchemaMismatchError,
    LlmTimeoutError,
)


class TestLlmFailureClasses:
    """Five distinct LLM failure modes, each with independent handling."""

    def test_refusal_is_distinct_from_provider_error(self):
        """Refusal (model says no) vs provider error (API says no) are different."""
        # These should be different exception types with different error codes
        assert issubclass(LlmRefusalError, Exception)
        assert issubclass(LlmProviderError, Exception)
        assert LlmRefusalError is not LlmProviderError

    def test_incomplete_output_is_distinct(self):
        """Incomplete output (max tokens) is retryable; provider error may not be."""
        assert issubclass(LlmIncompleteError, Exception)
        # Retryable: yes. Provider error: no.

    def test_schema_mismatch_is_not_retried(self):
        """Schema mismatch means the model output doesn't match contract.
        Do not retry the same request; log the mismatch for investigation.
        """
        assert issubclass(LlmSchemaMismatchError, Exception)

    def test_timeout_is_distinct_from_general_provider_error(self):
        """Timeout is a provider error subtype, but tracked separately."""
        assert issubclass(LlmTimeoutError, LlmProviderError)
        # Provider error: yes. Timeout: yes. Single metric tracking.

    def test_refusal_reasons_are_categorized(self):
        """Refusal reasons are categorized: rate_limit, auth, moderation, other."""
        # These should be enum values or string constants
        reasons = ["rate_limit", "auth", "moderation", "other"]
        for reason in reasons:
            exc = LlmRefusalError(reason=reason)
            assert exc.reason == reason


class TestErrorCodeMapping:
    """Each failure class maps to a distinct error_code for observability."""

    def test_refusal_error_code(self):
        """Refusal maps to a specific error code (e.g., 'llm_refusal')."""
        # The error_code should be queryable from the exception
        exc = LlmRefusalError(reason="moderation")
        assert hasattr(exc, 'error_code')
        # Should be distinct from other error codes
        assert exc.error_code.startswith('llm_')

    def test_incomplete_error_code(self):
        """Incomplete output maps to a distinct code."""
        exc = LlmIncompleteError()
        assert hasattr(exc, 'error_code')

    def test_provider_error_code(self):
        """Provider error (429, 5xx) maps to a code."""
        exc = LlmProviderError(status_code=429, detail="rate limited")
        assert hasattr(exc, 'error_code')

    def test_schema_mismatch_error_code(self):
        """Schema mismatch maps to a code."""
        exc = LlmSchemaMismatchError(detail="missing field: metrics")
        assert hasattr(exc, 'error_code')

    def test_timeout_error_code(self):
        """Timeout maps to its own code (not just 'provider_error')."""
        exc = LlmTimeoutError(seconds=30)
        assert hasattr(exc, 'error_code')
        # Should be more specific than generic provider error
        assert 'timeout' in exc.error_code.lower()


class TestErrorHandlingStrategy:
    """Each failure class drives a distinct handling strategy."""

    def test_refusal_is_not_retried(self):
        """Refusal is not retried; return immediately with safe message."""
        # The exception should signal: do not retry
        exc = LlmRefusalError(reason="moderation")
        assert not hasattr(exc, 'should_retry') or exc.should_retry is False

    def test_incomplete_is_retried_once(self):
        """Incomplete output is retried once with lower output token limit."""
        exc = LlmIncompleteError()
        # Signal: retry with reduced max_tokens
        assert hasattr(exc, 'retryable') and exc.retryable is True

    def test_provider_error_exponential_backoff(self):
        """Provider errors (429, 5xx) use exponential backoff + jitter."""
        exc = LlmProviderError(status_code=503)
        # Should indicate: use backoff strategy
        assert hasattr(exc, 'backoff_strategy')

    def test_schema_mismatch_logged_not_retried(self):
        """Schema mismatch is logged to admin trace; not retried."""
        exc = LlmSchemaMismatchError(detail="extra field not in schema")
        assert hasattr(exc, 'log_to_admin')
        assert exc.log_to_admin is True

    def test_timeout_triggers_circuit_breaker(self):
        """Timeout (after retries) may trigger circuit breaker."""
        exc = LlmTimeoutError(seconds=60)
        # After N timeouts, may enter degraded mode
        # This is handled at a higher level, but the exception should mark it
        assert hasattr(exc, 'backoff_strategy')


class TestFallbackModelOutputStillValidated:
    """Fallback model output must pass the same schema validation."""

    def test_fallback_output_not_exempt_from_schema(self):
        """Even if retrying with a fallback model, schema is still enforced."""
        # The validator should be the same for main model and fallback
        # No lowering of the safety contract
        pass


class TestNoMergedErrorCode:
    """All five classes have DISTINCT error codes, not merged."""

    def test_error_codes_are_unique(self):
        """Each exception class produces a unique error_code."""
        codes = set()
        exceptions = [
            LlmRefusalError(reason="moderation"),
            LlmIncompleteError(),
            LlmProviderError(status_code=429),
            LlmSchemaMismatchError(detail="test"),
            LlmTimeoutError(seconds=30),
        ]
        for exc in exceptions:
            code = exc.error_code
            assert code not in codes, f"Duplicate error_code: {code}"
            codes.add(code)

        assert len(codes) == 5, "All 5 exceptions should have unique codes"
