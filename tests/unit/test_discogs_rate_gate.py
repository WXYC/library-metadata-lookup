"""No-PG unit coverage for ``DiscogsRateGate`` fail-open behavior (LML#841).

The gate is the seam ``discogs/service.py`` calls in place of the raw
``AsyncLimiter``. Its contract:

  * flag OFF  → delegate straight to the local ``AsyncLimiter``; never touch PG.
  * flag ON, bucket healthy → spend a PG token (queue on empty, like the limiter).
  * flag ON, PG unreachable/erroring/slow → fail OPEN to the local limiter so a
    discogs-cache hiccup can never wedge the live-probe path.

These paths need no database: a fake bucket factory injects the failure mode and
a recording local limiter proves the fallback fired. The happy-path token spend
against real PG is covered in ``tests/integration/test_discogs_rate_bucket.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from config.settings import get_settings
from discogs.ratelimit import DiscogsRateGate
from entity.discogs_rate_bucket import TokenAcquisition

pytestmark = pytest.mark.asyncio


class _RecordingLimiter:
    """Stand-in for ``AsyncLimiter`` that counts fallback acquisitions."""

    def __init__(self) -> None:
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1


class _RaisingBucket:
    """PG bucket whose every acquire attempt raises (connection down / no row)."""

    def __init__(self) -> None:
        self.calls = 0

    async def try_acquire(self) -> TokenAcquisition:
        self.calls += 1
        raise RuntimeError("discogs-cache PG unreachable")


class _HangingBucket:
    """PG bucket that stalls past the gate timeout (slow/locked PG)."""

    async def try_acquire(self) -> TokenAcquisition:
        await asyncio.sleep(1.0)
        return TokenAcquisition(allowed=True, retry_after_s=0.0)


class _AllowingBucket:
    """Healthy PG bucket that immediately grants a token."""

    def __init__(self) -> None:
        self.calls = 0

    async def try_acquire(self) -> TokenAcquisition:
        self.calls += 1
        return TokenAcquisition(allowed=True, retry_after_s=0.0)


class _DeniedThenAllowedBucket:
    """Empty bucket that denies once (with a retry hint) then grants — models a
    saturated-but-healthy PG the gate must QUEUE on, not fail open around."""

    def __init__(self, retry_after_s: float) -> None:
        self.calls = 0
        self._retry_after_s = retry_after_s

    async def try_acquire(self) -> TokenAcquisition:
        self.calls += 1
        if self.calls == 1:
            return TokenAcquisition(allowed=False, retry_after_s=self._retry_after_s)
        return TokenAcquisition(allowed=True, retry_after_s=0.0)


@pytest.fixture
def rate_bucket_env(monkeypatch):
    """Set the LML#841 flags via env, clearing the settings LRU cache both ways."""

    def _apply(*, enabled: bool, timeout_s: float = 0.5):
        monkeypatch.setenv("DISCOGS_RATE_BUCKET_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("DISCOGS_RATE_BUCKET_TIMEOUT_S", str(timeout_s))
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


@pytest.fixture
def captured_tags(monkeypatch):
    """Capture ``sentry_sdk.set_tag`` calls made by the gate.

    Local-first pacing (LML#841 review, Finding 2) means the local limiter is
    now advanced on EVERY enabled call, so its acquire count no longer
    distinguishes the healthy path from a fail-open. The fail-open signal is the
    ``lml.discogs.rate_gate=fallback`` tag — the real production observable
    (LML#683) — so the tests assert on it directly."""
    tags: dict[str, str] = {}
    monkeypatch.setattr(
        "discogs.ratelimit.sentry_sdk.set_tag",
        lambda key, value: tags.__setitem__(key, value),
    )
    return tags


async def _factory_returning(bucket):
    async def factory():
        return bucket

    return factory


async def test_flag_off_delegates_to_local_and_skips_pg(rate_bucket_env):
    """With the flag OFF the gate must not build or call the PG bucket at all."""
    rate_bucket_env(enabled=False)
    local = _RecordingLimiter()
    built = False

    async def factory():
        nonlocal built
        built = True
        return _AllowingBucket()

    gate = DiscogsRateGate(local_limiter=local, bucket_factory=factory)
    await gate.acquire()

    assert local.acquired == 1
    assert built is False  # factory never invoked → PG untouched


async def test_flag_on_healthy_bucket_spends_pg_token(rate_bucket_env, captured_tags):
    rate_bucket_env(enabled=True)
    local = _RecordingLimiter()
    bucket = _AllowingBucket()
    gate = DiscogsRateGate(local_limiter=local, bucket_factory=await _factory_returning(bucket))

    await gate.acquire()

    assert bucket.calls == 1
    # Local-first (Finding 2): the local limiter is advanced on every enabled
    # call — a per-process cap that also keeps its burst reservoir drained. The
    # healthy path never tags fallback.
    assert local.acquired == 1
    assert "lml.discogs.rate_gate" not in captured_tags


async def test_pg_error_fails_open_to_local(rate_bucket_env, captured_tags):
    rate_bucket_env(enabled=True)
    local = _RecordingLimiter()
    bucket = _RaisingBucket()
    gate = DiscogsRateGate(local_limiter=local, bucket_factory=await _factory_returning(bucket))

    await gate.acquire()

    assert bucket.calls == 1
    # Already paced by the local-first acquire; fail-open adds no second acquire.
    assert local.acquired == 1
    assert captured_tags["lml.discogs.rate_gate"] == "fallback"


async def test_missing_pool_fails_open_to_local(rate_bucket_env, captured_tags):
    """A None pool (no discogs-cache configured) fails open, not hard-errors."""
    rate_bucket_env(enabled=True)
    local = _RecordingLimiter()
    gate = DiscogsRateGate(local_limiter=local, bucket_factory=await _factory_returning(None))

    await gate.acquire()

    assert local.acquired == 1
    assert captured_tags["lml.discogs.rate_gate"] == "fallback"


async def test_empty_bucket_queues_then_succeeds_without_fallback(rate_bucket_env, captured_tags):
    """A saturated-but-healthy bucket makes the gate SLEEP ``retry_after_s`` and
    re-try on PG — it must NOT fail open (that would over-issue past the shared
    budget). The per-round-trip timeout bounds each call, not the queue wait."""
    rate_bucket_env(enabled=True, timeout_s=0.5)
    local = _RecordingLimiter()
    bucket = _DeniedThenAllowedBucket(retry_after_s=0.02)
    gate = DiscogsRateGate(local_limiter=local, bucket_factory=await _factory_returning(bucket))

    loop = asyncio.get_running_loop()
    start = loop.time()
    await gate.acquire()
    elapsed = loop.time() - start

    assert bucket.calls == 2  # denied once, retried, granted
    assert local.acquired == 1  # local-first pace, then queued on PG
    assert "lml.discogs.rate_gate" not in captured_tags  # queued, never fell back
    assert elapsed >= 0.015  # actually slept the retry hint


async def test_degenerate_retry_after_is_capped(rate_bucket_env, captured_tags, monkeypatch):
    """LML#841 review (Finding 3): a pathological ``retry_after_s`` hint must not
    make the gate sleep for that whole duration. The queue sleep is clamped to
    ``_MAX_QUEUE_SLEEP_S`` so a bad hint can't wedge the live-probe tail."""
    rate_bucket_env(enabled=True, timeout_s=0.5)
    monkeypatch.setattr("discogs.ratelimit._MAX_QUEUE_SLEEP_S", 0.03)
    local = _RecordingLimiter()
    bucket = _DeniedThenAllowedBucket(retry_after_s=999.0)
    gate = DiscogsRateGate(local_limiter=local, bucket_factory=await _factory_returning(bucket))

    loop = asyncio.get_running_loop()
    start = loop.time()
    await gate.acquire()
    elapsed = loop.time() - start

    assert bucket.calls == 2  # denied (huge hint), capped-slept, retried, granted
    assert elapsed < 0.5  # clamped to _MAX_QUEUE_SLEEP_S, NOT 999s


async def test_slow_pg_times_out_and_fails_open(rate_bucket_env, captured_tags):
    """A single PG round-trip past ``timeout_s`` fails open — a slow/locked
    discogs-cache never wedges the live-probe tail."""
    rate_bucket_env(enabled=True, timeout_s=0.02)
    local = _RecordingLimiter()
    gate = DiscogsRateGate(
        local_limiter=local, bucket_factory=await _factory_returning(_HangingBucket())
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    await gate.acquire()
    elapsed = loop.time() - start

    assert local.acquired == 1
    assert captured_tags["lml.discogs.rate_gate"] == "fallback"
    assert elapsed < 0.5  # bounded by timeout_s, not the 1s hang
