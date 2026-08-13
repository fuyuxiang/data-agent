"""Resilience patterns: retries, backoff, circuit breaker."""

from __future__ import annotations

import random
import time
from enum import Enum


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject immediately
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreaker:
    """Stateful circuit breaker (not a singleton; per-client instance)."""

    def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 60) -> None:
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None

    def record_success(self) -> None:
        """Record a successful call."""
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def is_open(self) -> bool:
        """Check if the circuit is open (rejecting calls)."""
        if self.state != CircuitState.OPEN:
            return False

        # Check if timeout has elapsed; if so, try half-open
        if (
            self.last_failure_time is not None
            and time.time() - self.last_failure_time > self.timeout_seconds
        ):
            self.state = CircuitState.HALF_OPEN
            return False

        return True


def exponential_backoff(attempt: int, base: float = 1.0, max_wait: float = 32.0) -> float:
    """Calculate exponential backoff with jitter."""
    wait = min(base * (2 ** attempt), max_wait)
    # Add jitter: ±10% of wait time
    jitter = wait * random.uniform(-0.1, 0.1)
    return max(0.1, wait + jitter)
