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
def captured_posthog(monkeypatch):
    """Capture the unsampled PostHog fail-open counter events (LML#879).

    The Sentry ``fallback`` tag rides *sampled* transactions, and the dominant
    fail-open traffic (BS backfill floods) runs ``tracesSampleRate=0`` — so the
    tag systematically under-counts during exactly the floods it should catch.
    The PostHog event is the sampling-independent signal; these tests pin its
    name, distinct_id, and properties as the queryable contract.

    Pins ``ENABLE_TELEMETRY=true``: the emitter early-returns when telemetry is
    off, and the unit-wide credential scrub blanks keys but not this toggle, so
    an ambient ``false`` (shell or ``.env``) would otherwise fail the positive
    tests spuriously. Pins ``ENVIRONMENT`` to a sentinel so the environment
    property assertion proves the value flows from ``Settings.environment`` —
    comparing against ``get_settings().environment`` would read back the same
    cached object the emitter used and pass even if the emitter hard-coded or
    mis-sourced the value. The default ``raising=True`` keeps the patch seam
    honest — if the accessor ref ever moves (e.g. goes function-local), the
    patch errors instead of silently no-oping the negative tests.
    """
    monkeypatch.setenv("ENABLE_TELEMETRY", "true")
    monkeypatch.setenv("ENVIRONMENT", "unit-test-env")
    get_settings.cache_clear()
    events: list[dict] = []

    class _FakePosthog:
        def capture(self, *, distinct_id, event, properties):
            events.append({"distinct_id": distinct_id, "event": event, "properties": properties})

    monkeypatch.setattr(
        "discogs.ratelimit.get_posthog_client",
        lambda event_prefix: _FakePosthog(),
    )
    return events


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


# --- LML#879 Deliverable A: unsampled PostHog fail-open counter ---------------


@pytest.mark.parametrize(
    ("bucket_builder", "timeout_s", "expected_error_type"),
    [
        pytest.param(_RaisingBucket, 0.5, "RuntimeError", id="pg-error"),
        pytest.param(_HangingBucket, 0.02, "TimeoutError", id="pg-timeout"),
        pytest.param(lambda: None, 0.5, "RuntimeError", id="missing-pool"),
    ],
)
async def test_fail_open_emits_unsampled_counter(
    rate_bucket_env, captured_posthog, bucket_builder, timeout_s, expected_error_type
):
    """Every fail-open variant must emit the PostHog counter event with the FULL
    queryable contract shape — conventional distinct_id, stable event name, the
    exception type (so a PG outage, a slow PG, and a missing pool stay
    distinguishable from a code defect), and the reporting environment. One
    parametrized body so no variant's assertions can drift thinner than its
    siblings'."""
    rate_bucket_env(enabled=True, timeout_s=timeout_s)
    gate = DiscogsRateGate(
        local_limiter=_RecordingLimiter(),
        bucket_factory=await _factory_returning(bucket_builder()),
    )

    await gate.acquire()

    assert len(captured_posthog) == 1
    event = captured_posthog[0]
    assert event["distinct_id"] == "library-metadata-lookup-service"
    assert event["event"] == "discogs_rate_gate_fail_open"
    assert event["properties"]["error_type"] == expected_error_type
    assert event["properties"]["environment"] == "unit-test-env"


async def test_healthy_and_queueing_paths_emit_no_counter(rate_bucket_env, captured_posthog):
    """Neither a granted token nor an empty-but-healthy QUEUE is a fail-open —
    the counter must stay silent so a sustained non-zero rate is a trustworthy
    'bucket is silently not enforcing' signal during the enablement rollout."""
    rate_bucket_env(enabled=True)
    gate = DiscogsRateGate(
        local_limiter=_RecordingLimiter(),
        bucket_factory=await _factory_returning(_AllowingBucket()),
    )
    await gate.acquire()

    queueing_gate = DiscogsRateGate(
        local_limiter=_RecordingLimiter(),
        bucket_factory=await _factory_returning(_DeniedThenAllowedBucket(retry_after_s=0.01)),
    )
    await queueing_gate.acquire()

    assert [
        event for event in captured_posthog if event["event"] == "discogs_rate_gate_fail_open"
    ] == []


async def test_queueing_path_emits_queue_wait_event(rate_bucket_env, captured_posthog, monkeypatch):
    """A saturated-but-healthy bucket should emit the token-queue wait once it
    eventually grants a token. This is the LML#879 Deliverable B measurement
    surface for the N>=2 double-flood: fail-open stays silent, but the queue
    tail is queryable independently of Sentry trace sampling."""
    rate_bucket_env(enabled=True)
    monkeypatch.setattr("discogs.ratelimit.random.random", lambda: 0.0)
    gate = DiscogsRateGate(
        local_limiter=_RecordingLimiter(),
        bucket_factory=await _factory_returning(_DeniedThenAllowedBucket(retry_after_s=0.01)),
    )

    await gate.acquire()

    assert len(captured_posthog) == 1
    event = captured_posthog[0]
    assert event["distinct_id"] == "library-metadata-lookup-service"
    assert event["event"] == "discogs_rate_gate_queue_wait"
    assert event["properties"]["queue_sleeps"] == 1
    assert event["properties"]["wait_ms"] >= 5
    assert event["properties"]["environment"] == "unit-test-env"


async def test_flag_off_emits_no_counter(rate_bucket_env, captured_posthog):
    """Flag OFF never touches PG, so it can never fail open — counter stays 0."""
    rate_bucket_env(enabled=False)
    gate = DiscogsRateGate(
        local_limiter=_RecordingLimiter(),
        bucket_factory=await _factory_returning(_RaisingBucket()),
    )

    await gate.acquire()

    assert captured_posthog == []


async def test_telemetry_disabled_skips_counter_but_still_fails_open(
    rate_bucket_env, captured_posthog, captured_tags, monkeypatch
):
    """``ENABLE_TELEMETRY=false`` suppresses the PostHog emit (mirroring the DI
    dependencies' short-circuit) without disturbing the fail-open contract."""
    monkeypatch.setenv("ENABLE_TELEMETRY", "false")
    rate_bucket_env(enabled=True)
    local = _RecordingLimiter()
    gate = DiscogsRateGate(
        local_limiter=local, bucket_factory=await _factory_returning(_RaisingBucket())
    )

    await gate.acquire()

    assert captured_posthog == []
    assert local.acquired == 1
    assert captured_tags["lml.discogs.rate_gate"] == "fallback"


async def test_counter_emission_failure_never_breaks_fail_open(
    rate_bucket_env, captured_tags, monkeypatch
):
    """A broken PostHog client (accessor raising) must not turn a graceful
    fail-open into a hard error — telemetry is strictly best-effort here.

    Pins ``ENABLE_TELEMETRY=true`` (can't reuse ``captured_posthog`` — this test
    replaces the accessor): an ambient ``false`` would early-return before the
    exploding accessor and pass this test vacuously."""
    monkeypatch.setenv("ENABLE_TELEMETRY", "true")
    rate_bucket_env(enabled=True)

    def _exploding_accessor(event_prefix):
        raise RuntimeError("posthog accessor broken")

    monkeypatch.setattr("discogs.ratelimit.get_posthog_client", _exploding_accessor)
    local = _RecordingLimiter()
    gate = DiscogsRateGate(
        local_limiter=local, bucket_factory=await _factory_returning(_RaisingBucket())
    )

    await gate.acquire()  # must not raise

    assert local.acquired == 1
    assert captured_tags["lml.discogs.rate_gate"] == "fallback"
