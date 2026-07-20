"""``@pytest.mark.pg`` coverage for the shared Discogs token bucket (LML#841).

The token bucket lives in the LML-owned ``lml_cache.discogs_rate_bucket`` PG
table so every LML process (prod replicas + staging, which share the same
discogs-cache PG and the same Discogs token) draws rate permits from ONE row —
exact global enforcement, unlike the per-process ``AsyncLimiter``.

The correctness property that matters — *no over-issue under concurrency* —
lives in the single ``UPDATE … RETURNING`` primitive (``PgTokenBucket.try_acquire``),
so these tests drive that primitive against a real PG. The queue-until-a-token
loop and the fail-open behavior are gate *policy*, tested (with no PG) in
``tests/unit/test_discogs_rate_gate.py``.

Run with: pytest -m pg -v tests/integration/test_discogs_rate_bucket.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from entity.discogs_rate_bucket import (
    PgTokenBucket,
    RateBucketMissingRowError,
    TokenAcquisition,
    set_up_discogs_rate_bucket_schema,
)
from entity.sources import PgSource

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

# Slow enough that no meaningful refill happens inside a sub-second test window,
# so "burst then denied" and "no over-issue" assertions are deterministic.
_TINY_REFILL = 0.001


@pytest_asyncio.fixture
async def pg_source(pg_pool_large) -> PgSource:
    """A ``PgSource`` borrowing the large test pool (headroom for the gather)."""
    return PgSource(pool=pg_pool_large)


@pytest_asyncio.fixture
async def bucket_key(pg_source) -> str:
    """A unique bucket key per test, torn down afterwards.

    Isolates each test from every other and from any real seeded ``'discogs'``
    row, so the suite is order-independent and never mutates production data.
    """
    key = f"test_{uuid.uuid4().hex[:12]}"
    yield key
    await pg_source.execute("DELETE FROM lml_cache.discogs_rate_bucket WHERE bucket_key = $1", key)


async def _seed(pg_source: PgSource, key: str, *, capacity: float, refill: float) -> PgTokenBucket:
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=key, capacity=capacity, refill_per_sec=refill
    )
    return PgTokenBucket(pg_source, bucket_key=key)


async def _row(pg_source: PgSource, key: str) -> dict:
    return await pg_source.fetchone(
        "SELECT tokens, capacity, refill_per_sec "
        "FROM lml_cache.discogs_rate_bucket WHERE bucket_key = $1",
        key,
    )


async def test_schema_bootstrap_is_idempotent(pg_source, bucket_key):
    """Running the bootstrap twice leaves exactly one row seeded from settings."""
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=bucket_key, capacity=5, refill_per_sec=_TINY_REFILL
    )
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=bucket_key, capacity=5, refill_per_sec=_TINY_REFILL
    )
    count = await pg_source.fetchone(
        "SELECT count(*) AS n FROM lml_cache.discogs_rate_bucket WHERE bucket_key = $1",
        bucket_key,
    )
    assert count["n"] == 1
    row = await _row(pg_source, bucket_key)
    assert row["capacity"] == 5
    assert row["refill_per_sec"] == pytest.approx(_TINY_REFILL)
    # Seeded full.
    assert row["tokens"] == pytest.approx(5, abs=0.01)


async def test_reseed_does_not_clobber_existing_capacity(pg_source, bucket_key):
    """ON CONFLICT DO NOTHING: a second bootstrap with a different budget is a no-op.

    Prod owns the budget value; a staging boot with a different ``discogs_rate_limit``
    must not oscillate the shared row (plan §Risks).
    """
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=bucket_key, capacity=5, refill_per_sec=_TINY_REFILL
    )
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=bucket_key, capacity=99, refill_per_sec=1.0
    )
    row = await _row(pg_source, bucket_key)
    assert row["capacity"] == 5


async def test_try_acquire_spends_one_token(pg_source, bucket_key):
    bucket = await _seed(pg_source, bucket_key, capacity=5, refill=_TINY_REFILL)
    res = await bucket.try_acquire()
    assert isinstance(res, TokenAcquisition)
    assert res.allowed is True
    assert res.retry_after_s == pytest.approx(0.0)
    row = await _row(pg_source, bucket_key)
    # One token spent (minus negligible refill).
    assert 3.9 < row["tokens"] < 5.0


async def test_burst_exhausts_then_denies_with_retry_after(pg_source, bucket_key):
    bucket = await _seed(pg_source, bucket_key, capacity=3, refill=_TINY_REFILL)
    for _ in range(3):
        assert (await bucket.try_acquire()).allowed is True
    denied = await bucket.try_acquire()
    assert denied.allowed is False
    assert denied.retry_after_s > 0.0


async def test_tokens_refill_over_time(pg_source, bucket_key):
    # Fast refill (100/s => a token every 10ms), capacity 1.
    bucket = await _seed(pg_source, bucket_key, capacity=1, refill=100.0)
    assert (await bucket.try_acquire()).allowed is True
    assert (await bucket.try_acquire()).allowed is False  # drained
    await asyncio.sleep(0.05)  # 0.05 * 100 = 5 tokens accrue, capped at capacity 1
    assert (await bucket.try_acquire()).allowed is True


async def test_concurrent_acquires_never_over_issue(pg_source, bucket_key):
    """The core invariant: N concurrent try_acquire against one row yield exactly
    ``capacity`` allows — the FOR UPDATE serialization makes refill/spend atomic."""
    capacity = 5
    bucket = await _seed(pg_source, bucket_key, capacity=capacity, refill=_TINY_REFILL)
    results = await asyncio.gather(*(bucket.try_acquire() for _ in range(capacity + 10)))
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity


async def test_two_instances_share_one_row(pg_source, bucket_key):
    """Two independent PgTokenBucket instances (staging + prod) are collectively
    bounded by ``capacity`` — enforcement is on the row, not the instance."""
    capacity = 4
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key=bucket_key, capacity=capacity, refill_per_sec=_TINY_REFILL
    )
    prod = PgTokenBucket(pg_source, bucket_key=bucket_key)
    staging = PgTokenBucket(pg_source, bucket_key=bucket_key)
    both = [prod, staging] * (capacity + 3)  # > capacity acquires split across instances
    results = await asyncio.gather(*(b.try_acquire() for b in both))
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity


async def test_try_acquire_missing_row_raises(pg_source):
    """An unseeded key has no row; try_acquire raises so the gate can fail open."""
    await set_up_discogs_rate_bucket_schema(
        pg_source, bucket_key="test_seeded_other", capacity=1, refill_per_sec=_TINY_REFILL
    )
    orphan = PgTokenBucket(pg_source, bucket_key=f"test_absent_{uuid.uuid4().hex[:8]}")
    with pytest.raises(RateBucketMissingRowError):
        await orphan.try_acquire()
