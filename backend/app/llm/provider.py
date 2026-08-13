"""LLM provider abstraction with failure classification.

Five distinct failure modes, each with independent error_code:
1. Refusal (model says no) - not retried
2. Incomplete (max tokens) - retried once with lower limit
3. Provider error (429, 5xx) - exponential backoff + jitter
4. Schema mismatch (output invalid) - logged, not retried
5. Timeout - exponential backoff, may trigger circuit breaker
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


# ===== FAILURE CLASSES =====


class RefusalReason(str, Enum):
    """Reason the model refused to generate an intent."""
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    MODERATION = "moderation"
    OTHER = "other"


class LlmRefusalError(Exception):
    """Model explicitly refused (e.g., moderation, explicit refusal)."""

    def __init__(self, reason: str | RefusalReason = RefusalReason.OTHER) -> None:
        self.reason = reason if isinstance(reason, RefusalReason) else RefusalReason(reason)
        self.should_retry = False
        self.error_code = f"llm_refusal_{self.reason.value}"
        super().__init__(f"LLM refusal: {self.reason.value}")


class LlmIncompleteError(Exception):
    """Output incomplete (e.g., max tokens reached)."""

    def __init__(self) -> None:
        self.retryable = True
        self.error_code = "llm_incomplete"
        super().__init__("LLM output incomplete (max tokens)")


class LlmProviderError(Exception):
    """Provider error (429, 5xx, network, etc)."""

    def __init__(self, status_code: int | None = None, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        self.backoff_strategy = "exponential"  # + jitter
        self.error_code = f"llm_provider_error_{status_code}" if status_code else "llm_provider_error"
        super().__init__(f"LLM provider error: {detail}")


class LlmSchemaMismatchError(Exception):
    """Output doesn't match expected schema."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        self.log_to_admin = True  # Log to admin-level trace
        self.should_retry = False
        self.error_code = "llm_schema_mismatch"
        super().__init__(f"LLM schema mismatch: {detail}")


class LlmTimeoutError(LlmProviderError):
    """Timeout waiting for LLM response."""

    def __init__(self, seconds: int) -> None:
        super().__init__(detail=f"timeout after {seconds}s")
        self.seconds = seconds
        self.backoff_strategy = "exponential"
        self.error_code = "llm_timeout"


# ===== PROVIDER INTERFACE =====


@dataclass
class LlmCompletion:
    """Response from LLM."""
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class IntentModel(ABC):
    """Interface for LLM-based intent generation.

    Abstracts away OpenAI SDK specifics; business code uses only this interface.
    """

    @abstractmethod
    async def complete(self, system: str, user: str) -> LlmCompletion:
        """Generate a completion given system and user prompts.

        May raise one of: LlmRefusalError, LlmIncompleteError,
        LlmProviderError, LlmSchemaMismatchError, LlmTimeoutError.
        """
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier (e.g., 'gpt-4-turbo', 'gpt-4o')."""
        ...
