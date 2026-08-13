"""Tests for S6 result cache and async job queue."""

import pytest


pytestmark = pytest.mark.no_db


def _make_query_result(rows: int = 10):
    """Create a minimal QueryResult-like object for tests."""
    from app.execution.runner import QueryResult

    return QueryResult(
        columns=("id", "amount"),
        rows=tuple((i, i * 10) for i in range(rows)),
        row_count=rows,
        truncated=False,
        elapsed_ms=100,
    )


class TestCacheKey:
    """Test cache key composition."""

    def test_same_inputs_same_key(self):
        """Same inputs produce the same key."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )

        assert k1.to_hex() == k2.to_hex()

    def test_different_principal_different_key(self):
        """Different principal produces different key (no cross-user leaks)."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        k2 = make_cache_key(
            "plan-1", principal_id=2, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )

        assert k1.to_hex() != k2.to_hex()

    def test_different_tenant_different_key(self):
        """Different tenant produces different key."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t2",
            policy_hash="p1", semantic_revision_id=1,
        )

        assert k1.to_hex() != k2.to_hex()

    def test_different_policy_different_key(self):
        """Different policy_hash produces different key (no policy leak)."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p2", semantic_revision_id=1,
        )

        assert k1.to_hex() != k2.to_hex()

    def test_different_revision_different_key(self):
        """Different semantic_revision_id produces different key."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=2,
        )

        assert k1.to_hex() != k2.to_hex()

    def test_parameters_included_in_key(self):
        """Different parameter bindings produce different keys."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
            parameters={"region": "east"},
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
            parameters={"region": "west"},
        )

        assert k1.to_hex() != k2.to_hex()

    def test_parameter_order_doesnt_matter(self):
        """Parameter ordering doesn't affect key (sorted)."""
        from app.observability.cache import make_cache_key

        k1 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
            parameters={"a": 1, "b": 2},
        )
        k2 = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
            parameters={"b": 2, "a": 1},
        )

        assert k1.to_hex() == k2.to_hex()


class TestResultCache:
    """Test ResultCache behaviour."""

    def test_initial_cache_is_empty(self):
        """New cache has no entries."""
        from app.observability.cache import ResultCache

        cache = ResultCache()
        assert cache.size() == 0

    def test_put_then_lookup_hits(self):
        """Putting an entry allows lookup to hit."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        result = _make_query_result()
        cache.put(key, result)

        lookup = cache.lookup(key)
        assert lookup.hit is True
        assert lookup.entry is not None
        assert lookup.entry.value.row_count == 10

    def test_miss_returns_no_entry(self):
        """Missing key returns miss."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )

        lookup = cache.lookup(key)
        assert lookup.hit is False
        assert lookup.entry is None

    def test_expired_entry_misses(self):
        """Expired entry is treated as miss and removed."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache(default_ttl_seconds=10)
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        result = _make_query_result()
        cache.put(key, result, ttl_seconds=1)

        # Force "now" to be 100 seconds in the future
        import time
        future = time.time() + 100
        lookup = cache.lookup(key, now=future)
        assert lookup.hit is False
        # Expired entry should have been removed
        assert cache.size() == 0

    def test_hit_count_increments(self):
        """hit_count increments on each lookup."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        cache.put(key, _make_query_result())

        # 3 lookups, then 4th (which itself increments) -> 4
        for _ in range(3):
            cache.lookup(key)

        lookup = cache.lookup(key)
        assert lookup.entry.hit_count == 4  # 3 prior + 1 current

    def test_invalidate_specific_entry(self):
        """invalidate removes the specific entry."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        cache.put(key, _make_query_result())

        assert cache.invalidate(key) is True
        assert cache.size() == 0

    def test_invalidate_nonexistent_returns_false(self):
        """invalidate returns False for missing key."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        key = make_cache_key(
            "plan-1", principal_id=1, tenant_id="t1",
            policy_hash="p1", semantic_revision_id=1,
        )
        assert cache.invalidate(key) is False

    def test_invalidate_all(self):
        """invalidate_all drops everything."""
        from app.observability.cache import ResultCache, make_cache_key

        cache = ResultCache()
        for i in range(5):
            key = make_cache_key(
                f"plan-{i}", principal_id=1, tenant_id="t1",
                policy_hash="p1", semantic_revision_id=1,
            )
            cache.put(key, _make_query_result())

        assert cache.size() == 5
        count = cache.invalidate_all()
        assert count == 5
        assert cache.size() == 0


class TestAsyncJobQueue:
    """Test async job queue (S6)."""

    def test_enqueue_creates_job(self):
        """enqueue creates a job in PENDING state."""
        from app.observability.cache import AsyncJobQueue, AsyncJobState

        queue = AsyncJobQueue()
        job = queue.enqueue("eval_run", {"run_id": "r1"})

        assert job.state == AsyncJobState.PENDING
        assert job.kind == "eval_run"
        assert job.payload == {"run_id": "r1"}

    def test_get_returns_enqueued_job(self):
        """get returns a previously enqueued job."""
        from app.observability.cache import AsyncJobQueue

        queue = AsyncJobQueue()
        job = queue.enqueue("eval_run", {})

        retrieved = queue.get(job.id)
        assert retrieved is not None
        assert retrieved.id == job.id

    def test_get_unknown_returns_none(self):
        """get returns None for unknown job_id."""
        from app.observability.cache import AsyncJobQueue

        queue = AsyncJobQueue()
        assert queue.get("nonexistent") is None

    def test_mark_running(self):
        """mark_running transitions to RUNNING."""
        from app.observability.cache import AsyncJobQueue, AsyncJobState

        queue = AsyncJobQueue()
        job = queue.enqueue("eval_run", {})

        queue.mark_running(job.id)
        retrieved = queue.get(job.id)
        assert retrieved.state == AsyncJobState.RUNNING
        assert retrieved.started_at is not None

    def test_mark_done(self):
        """mark_done transitions to DONE with result."""
        from app.observability.cache import AsyncJobQueue, AsyncJobState

        queue = AsyncJobQueue()
        job = queue.enqueue("eval_run", {})

        queue.mark_running(job.id)
        queue.mark_done(job.id, result={"score": 0.95})

        retrieved = queue.get(job.id)
        assert retrieved.state == AsyncJobState.DONE
        assert retrieved.result == {"score": 0.95}
        assert retrieved.completed_at is not None

    def test_mark_failed(self):
        """mark_failed transitions to FAILED with error."""
        from app.observability.cache import AsyncJobQueue, AsyncJobState

        queue = AsyncJobQueue()
        job = queue.enqueue("eval_run", {})

        queue.mark_failed(job.id, error="connection lost")
        retrieved = queue.get(job.id)
        assert retrieved.state == AsyncJobState.FAILED
        assert retrieved.error == "connection lost"

    def test_list_by_state(self):
        """list_by_state returns jobs in the given state."""
        from app.observability.cache import AsyncJobQueue, AsyncJobState

        queue = AsyncJobQueue()
        j1 = queue.enqueue("eval_run", {})
        j2 = queue.enqueue("deep_analysis", {})
        j3 = queue.enqueue("vq_revalidate", {})

        queue.mark_done(j1.id, result={})
        queue.mark_done(j2.id, result={})
        # j3 stays pending

        done = queue.list_by_state(AsyncJobState.DONE)
        pending = queue.list_by_state(AsyncJobState.PENDING)

        assert len(done) == 2
        assert len(pending) == 1
        assert j3 in pending

    def test_multiple_jobs_have_unique_ids(self):
        """Multiple enqueued jobs get unique IDs."""
        from app.observability.cache import AsyncJobQueue

        queue = AsyncJobQueue()
        jobs = [queue.enqueue("k", {}) for _ in range(5)]
        ids = [j.id for j in jobs]
        assert len(set(ids)) == 5
