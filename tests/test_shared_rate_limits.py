import time
import pytest
from assay.auth import RateLimiter


class MockRedisPipeline:
    def __init__(self, store):
        self.store = store
        self.key = None

    def incr(self, key):
        self.key = key
        self.store[key] = self.store.get(key, 0) + 1
        return self

    def expire(self, key, seconds):
        return self

    def execute(self):
        return [self.store[self.key]]


class MockRedisClient:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return MockRedisPipeline(self.store)

    def ttl(self, key):
        return 45


def test_in_memory_rate_limiting():
    limiter = RateLimiter(per_minute=2)
    assert limiter.check("user_1") is None
    assert limiter.check("user_1") is None
    retry_after = limiter.check("user_1")
    assert retry_after is not None and retry_after > 0


def test_shared_redis_rate_limiting_across_replicas():
    # Simulate shared Redis storage shared across Replica A and Replica B
    shared_redis = MockRedisClient()

    replica_a = RateLimiter(per_minute=3, redis_client=shared_redis)
    replica_b = RateLimiter(per_minute=3, redis_client=shared_redis)

    # 2 requests on replica A
    assert replica_a.check("org_key_1") is None
    assert replica_a.check("org_key_1") is None

    # 1 request on replica B: total is now 3
    assert replica_b.check("org_key_1") is None

    # 4th request on either replica should be blocked by shared limit!
    retry_a = replica_a.check("org_key_1")
    assert retry_a is not None and retry_a == 45

    retry_b = replica_b.check("org_key_1")
    assert retry_b is not None and retry_b == 45


def test_redis_failure_falls_back_to_in_memory():
    class BrokenRedisClient:
        def pipeline(self):
            raise ConnectionError("Redis server went away")

    limiter = RateLimiter(per_minute=2, redis_client=BrokenRedisClient())
    # Should not raise; falls back gracefully to in-memory tracking
    assert limiter.check("user_2") is None
    assert limiter.check("user_2") is None
    assert limiter.check("user_2") is not None
