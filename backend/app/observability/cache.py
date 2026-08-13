"""Result caching (S6).

Cache key composition rules (S6 spec):
- canonical_plan_hash: same plan -> same SQL, so always included
- principal_id + tenant_id: results must NOT leak across users
- policy_hash: same plan with different policy must NOT share results
- semantic_revision_id: plans from old revisions are stale
- parameter_bindings: parametric assets need param-aware keys

The result cache returns:
- HIT: cached QueryResult, plus the canonical plan it was built from
  (for citation consistency)
- MISS: caller compiles fresh, then writes back

Cache is keyed by content; same inputs always produce the same key.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class CacheKey:
    """All inputs that determine whether a cached result is reusable.

    Two requests with the same CacheKey MUST produce the same QueryResult.
    """

    canonical_plan_hash: str
    principal_id: int
    tenant_id: str
    policy_hash: str
    semantic_revision_id: int
    parameter_bindings: tuple[tuple[str, Any], ...] = ()  # Sorted for stability

    def to_hex(self) -> str:
        """Stable, content-addressed cache key."""
        payload = {
            "plan_hash": self.canonical_plan_hash,
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "policy_hash": self.policy_hash,
            "revision_id": self.semantic_revision_id,
            "params": dict(self.parameter_bindings),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    """A cached QueryResult with provenance for citation."""

    key_hex: str
    value: "QueryResult"  # Forward reference to avoid circular import
    created_at: float  # epoch seconds
    expires_at: float  # epoch seconds
    hit_count: int = 0


@dataclass(frozen=True)
class CacheLookup:
    """Result of a cache lookup."""

    hit: bool
    entry: Optional[CacheEntry] = None
    key_hex: str = ""


class ResultCache:
    """In-memory result cache (placeholder; full impl uses Redis).

    The interface is what matters here. Tests exercise the key composition,
    hit/miss behaviour, and TTL expiry. Backend implementations plug in
    via the same interface.
    """

    def __init__(self, default_ttl_seconds: int = 300):
        self._store: dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl_seconds

    def lookup(
        self, key: CacheKey, *, now: float | None = None
    ) -> CacheLookup:
        current = now or time.time()
        key_hex = key.to_hex()
        entry = self._store.get(key_hex)
        if entry is None:
            return CacheLookup(hit=False, key_hex=key_hex)
        if entry.expires_at <= current:
            # Expired; treat as miss and drop
            self._store.pop(key_hex, None)
            return CacheLookup(hit=False, key_hex=key_hex)
        # Hit; bump count
        self._store[key_hex] = CacheEntry(
            key_hex=entry.key_hex,
            value=entry.value,
            created_at=entry.created_at,
            expires_at=entry.expires_at,
            hit_count=entry.hit_count + 1,
        )
        return CacheLookup(hit=True, entry=self._store[key_hex], key_hex=key_hex)

    def put(
        self, key: CacheKey, value: "QueryResult", *, ttl_seconds: int | None = None
    ) -> None:
        current = time.time()
        ttl = ttl_seconds or self._default_ttl
        entry = CacheEntry(
            key_hex=key.to_hex(),
            value=value,
            created_at=current,
            expires_at=current + ttl,
        )
        self._store[key.to_hex()] = entry

    def invalidate(self, key: CacheKey) -> bool:
        """Remove a specific entry. Returns True if it existed."""
        return self._store.pop(key.to_hex(), None) is not None

    def invalidate_all(self) -> int:
        """Drop everything. Returns count removed."""
        count = len(self._store)
        self._store.clear()
        return count

    def size(self) -> int:
        return len(self._store)


def make_cache_key(
    canonical_plan_hash: str,
    *,
    principal_id: int,
    tenant_id: str,
    policy_hash: str,
    semantic_revision_id: int,
    parameters: dict[str, Any] | None = None,
) -> CacheKey:
    """Convenience factory for cache keys with sorted parameter bindings."""
    sorted_params = (
        tuple(sorted((parameters or {}).items(), key=lambda kv: kv[0]))
    )
    return CacheKey(
        canonical_plan_hash=canonical_plan_hash,
        principal_id=principal_id,
        tenant_id=tenant_id,
        policy_hash=policy_hash,
        semantic_revision_id=semantic_revision_id,
        parameter_bindings=sorted_params,
    )


# --- Async worker queue ----------------------------------------------------

class AsyncJobState(str):
    """Job lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AsyncJob:
    """An async job descriptor (eval, deep analysis, VQ re-validation)."""

    id: str
    kind: str  # "eval_run" | "deep_analysis" | "vq_revalidate" | "drift_scan"
    payload: dict[str, Any]
    state: str = AsyncJobState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: Any = None
    error: str | None = None


class AsyncJobQueue:
    """In-process async job queue (S6 spec §4).

    Production uses Redis/RQ; this is a minimal interface for tests
    and the API to share a contract.
    """

    def __init__(self):
        self._jobs: dict[str, AsyncJob] = {}
        self._counter = 0

    def enqueue(self, kind: str, payload: dict[str, Any]) -> AsyncJob:
        self._counter += 1
        job = AsyncJob(
            id=f"job-{self._counter}",
            kind=kind,
            payload=payload,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> AsyncJob | None:
        return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = AsyncJob(
            id=job.id, kind=job.kind, payload=job.payload,
            state=AsyncJobState.RUNNING,
            created_at=job.created_at,
            started_at=time.time(),
        )

    def mark_done(self, job_id: str, result: Any) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = AsyncJob(
            id=job.id, kind=job.kind, payload=job.payload,
            state=AsyncJobState.DONE,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=time.time(),
            result=result,
        )

    def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = AsyncJob(
            id=job.id, kind=job.kind, payload=job.payload,
            state=AsyncJobState.FAILED,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=time.time(),
            error=error,
        )

    def list_by_state(self, state: str) -> tuple[AsyncJob, ...]:
        return tuple(j for j in self._jobs.values() if j.state == state)
