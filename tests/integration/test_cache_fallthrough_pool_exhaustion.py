"""Integration tests for the cache fallthrough cool-down under real pool exhaustion.

Closes the remaining acceptance criterion on WXYC/library-metadata-lookup#324
(deferred from PR #416): a real-PostgreSQL-backed test that exercises the
actual exception class produced by Railway pool exhaustion and confirms the
``discogs/fallthrough.py`` cool-down arms end-to-end.

Why these tests exist
---------------------
The unit tests in ``tests/unit/test_fallthrough.py::TestCoolDown`` pin the
seam's behavior against simulated ``asyncpg`` exceptions. The seam responds
correctly to a ``PostgresConnectionError`` we hand it from a mock — but the
exception classes asyncpg *actually* raises under pool exhaustion drift across
versions, and we needed a real failure mode to close the contract.

Empirical finding (asyncpg 0.31.0, surfaced by this test file)
--------------------------------------------------------------
Under a deliberately-undersized pool (``min_size=1, max_size=1``) with a
short pool-acquire timeout, the raw exception ``asyncpg`` raises is
``builtins.TimeoutError`` (aliased as ``asyncio.TimeoutError`` in Py3.11+).
That class is **not** in ``_ARMING_EXCEPTIONS`` — and ``test_asyncio_timeout_does_not_arm_cool_down``
in the unit suite explicitly pins that ``TimeoutError`` is treated as a
per-request timeout, not a "DB unreachable" signal.

The cool-down still fires in prod because :class:`discogs.cache_service.DiscogsCacheService`
wraps every cache-leg query in a broad ``try/except Exception`` and re-raises
as :class:`CacheUnavailableError` (which **is** in ``_ARMING_EXCEPTIONS``).
So the seam-level contract holds: under real pool exhaustion in prod, the
cool-down arms via the ``CacheUnavailableError`` path, not the raw asyncpg
class.

These tests pin both halves of that contract:

1. ``test_real_pool_exhaustion_raises_arming_exception`` — exercises a real
   ``DiscogsCacheService.get_release`` against an exhausted pool and asserts
   the raised exception ``isinstance(exc, _ARMING_EXCEPTIONS)``, while also
   capturing the raw asyncpg class via ``exc.__cause__`` for diagnostics.
2. ``test_end_to_end_get_release_falls_back_on_exhausted_pool`` — wires
   ``DiscogsService`` against an exhausted pool with a mocked API path,
   confirms the fall-back response is returned, the cool-down is armed, the
   next call skips the cache leg entirely, and the Sentry transaction carries
   ``data.cache_fallback_fired``.

Why ``release`` table presence doesn't matter
---------------------------------------------
Under genuine pool exhaustion the query never reaches the server — the
acquire blocks at the pool layer and the wrapper's ``asyncio.wait_for`` fires
``TimeoutError`` before any SQL is sent. So these tests run identically on
CI's plain ``postgres:16-alpine`` (no schema) and on a fully-loaded local
discogs-cache.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio
import sentry_sdk

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.fallthrough import (
    _ARMING_EXCEPTIONS,
    _COOL_DOWN_SECONDS,
    _cool_down_active,
    _reset_cool_down_for_tests,
)
from discogs.memory_cache import RELEASE_CACHE
from discogs.models import ReleaseMetadataResponse, TrackItem

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)

# Short enough to fail fast, long enough that the contender is reliably parked
# behind Task A's transaction before its acquire deadline fires. 0.5s comfortably
# bridges the gap on any CI runner.
_POOL_ACQUIRE_TIMEOUT_SECONDS = 0.5


class _TimeoutPoolWrapper:
    """Proxy an asyncpg pool, capping every coroutine call with ``asyncio.wait_for``.

    ``asyncpg.Pool.fetchrow`` / ``fetch`` / ``fetchval`` accept a ``timeout=``
    kwarg, but it only governs the *per-query* command timeout — the inner
    ``self.acquire()`` call is unbounded. Under pool exhaustion the wait sits
    on acquire indefinitely (or until the pool's ``create_pool(timeout=10)``
    connect timeout fires, which is much longer than a test wants).

    Wrapping each call in ``asyncio.wait_for`` makes pool exhaustion surface as
    a clean :class:`TimeoutError` within a bounded window, matching the
    deliberately-undersized-pool scenario the AC describes.

    Forwards every coroutine-returning attribute (``fetchrow``, ``fetch``,
    ``fetchval``, ``execute``, ...); non-coroutine attributes pass through
    unwrapped. New cache-service call sites work automatically.
    """

    def __init__(self, pool: asyncpg.Pool, timeout: float):
        self._pool = pool
        self._timeout = timeout

    def __getattr__(self, name: str):
        attr = getattr(self._pool, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def _timed(*args, **kwargs):
            return await asyncio.wait_for(attr(*args, **kwargs), timeout=self._timeout)

        return _timed


@pytest_asyncio.fixture
async def real_pool():
    """A real ``min_size=1, max_size=1`` asyncpg pool against the test PG container.

    Skips when no PG is reachable so the test self-skips locally without one.
    """
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=1)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def _reset_cool_down_and_l1_cache():
    """Cool-down + L1 release cache are both process-wide; reset to keep tests independent.

    Mirrors the autouse fixture in ``tests/unit/test_fallthrough.py``.
    """
    _reset_cool_down_for_tests()
    RELEASE_CACHE.clear()
    yield
    _reset_cool_down_for_tests()
    RELEASE_CACHE.clear()


async def _hold_single_connection(
    pool: asyncpg.Pool, acquired: asyncio.Event, release: asyncio.Event
) -> None:
    """Task body: grab the pool's one connection inside a transaction and park.

    Sets ``acquired`` once the connection is in hand so the contending task
    can deterministically start its blocked call after the pool is genuinely
    exhausted; waits on ``release`` until the test has finished its
    assertions. Long-running transaction matches the dedup-swap / hot-path
    contention shape that motivates the ``_ARMING_EXCEPTIONS`` set.
    """
    async with pool.acquire() as conn, conn.transaction():
        # Touch the connection so the transaction is genuinely open server-side.
        await conn.execute("SELECT 1")
        acquired.set()
        # Bound the hold so a buggy test can't leak a held conn into the next test.
        try:
            await asyncio.wait_for(release.wait(), timeout=30.0)
        except TimeoutError:
            pass


@pytest.mark.pg
class TestPoolExhaustionRaisesArmingException:
    """AC #1 from WXYC/library-metadata-lookup#418.

    Pins that under real pool exhaustion the seam-visible exception class
    is in ``_ARMING_EXCEPTIONS``. The cache-service wrapper is the load-
    bearing piece: it catches the raw asyncpg ``TimeoutError`` and re-raises
    as :class:`CacheUnavailableError`, which the fallthrough seam recognizes
    as a cool-down trigger.
    """

    @pytest.mark.asyncio
    async def test_real_pool_exhaustion_raises_arming_exception(self, real_pool):
        """Real pool exhaustion through ``DiscogsCacheService.get_release`` →
        ``CacheUnavailableError`` (in ``_ARMING_EXCEPTIONS``); raw cause is
        ``TimeoutError`` (asyncpg's actual class under exhaustion).
        """
        timed_pool = _TimeoutPoolWrapper(real_pool, timeout=_POOL_ACQUIRE_TIMEOUT_SECONDS)
        cache_service = DiscogsCacheService(timed_pool)

        acquired = asyncio.Event()
        release = asyncio.Event()

        hog = asyncio.create_task(_hold_single_connection(real_pool, acquired, release))
        try:
            await acquired.wait()

            with pytest.raises(CacheUnavailableError) as exc_info:
                # ID is arbitrary: the call blocks at the acquire step, the
                # query never reaches the server.
                await cache_service.get_release(28138)

            # The seam recognizes this class as a cool-down arming trigger.
            assert isinstance(exc_info.value, _ARMING_EXCEPTIONS), (
                f"Expected {type(exc_info.value).__name__} to be in _ARMING_EXCEPTIONS; "
                f"the seam would not arm a cool-down for this class. "
                f"Set: {[c.__name__ for c in _ARMING_EXCEPTIONS]}"
            )

            # Diagnostic: pin the raw asyncpg class for future-asyncpg-version
            # drift detection. Empirically TimeoutError; the cache service's
            # broad except wraps it.
            cause = exc_info.value.__cause__
            assert cause is not None, (
                "DiscogsCacheService.get_release should chain the underlying asyncpg "
                "exception via `raise CacheUnavailableError(...) from e`."
            )
            assert isinstance(cause, TimeoutError), (
                f"Expected raw asyncpg class under pool exhaustion to be TimeoutError; "
                f"got {type(cause).__module__}.{type(cause).__qualname__}. "
                "If this changes in a future asyncpg release, audit "
                "discogs/fallthrough._ARMING_EXCEPTIONS for the new class."
            )
        finally:
            release.set()
            await hog


@pytest.mark.pg
class TestEndToEndCoolDownOnExhaustedPool:
    """AC #2 from WXYC/library-metadata-lookup#418.

    Wires the full ``DiscogsService.get_release`` path against the exhausted
    pool. Mocks the Discogs API leg so the test doesn't need network or a
    Discogs token. Asserts the four end-to-end behaviors the seam promises:

    1. The response shape is the API-fallback shape (``cached=False``).
    2. The cool-down is armed after the first call.
    3. The next call within the cool-down window skips the cache leg
       entirely (verified via a spy on ``DiscogsCacheService.get_release``).
    4. ``data.cache_fallback_fired`` is projected onto the active Sentry
       transaction.
    """

    @pytest.mark.asyncio
    async def test_end_to_end_get_release_falls_back_on_exhausted_pool(self, real_pool):
        timed_pool = _TimeoutPoolWrapper(real_pool, timeout=_POOL_ACQUIRE_TIMEOUT_SECONDS)
        cache_service = DiscogsCacheService(timed_pool)

        # Spy on get_release so we can count cache-leg invocations across calls.
        real_get_release = cache_service.get_release
        cache_spy = MagicMock(side_effect=real_get_release)
        cache_service.get_release = cache_spy  # type: ignore[method-assign]

        # Mock the Discogs API leg. Two different IDs so the L1 in-memory
        # cache (RELEASE_CACHE) doesn't short-circuit the second call.
        api_payloads = {
            101: ReleaseMetadataResponse(
                release_id=101,
                title="Aluminum Tunes",
                artist="Stereolab",
                year=1998,
                release_url="https://www.discogs.com/release/101",
                tracklist=[TrackItem(position="1", title="Pop Quiz", artists=["Stereolab"])],
                cached=False,
            ),
            102: ReleaseMetadataResponse(
                release_id=102,
                title="Dots and Loops",
                artist="Stereolab",
                year=1997,
                release_url="https://www.discogs.com/release/102",
                tracklist=[TrackItem(position="1", title="Brakhage", artists=["Stereolab"])],
                cached=False,
            ),
        }

        acquired = asyncio.Event()
        release = asyncio.Event()
        hog = asyncio.create_task(_hold_single_connection(real_pool, acquired, release))

        try:
            await acquired.wait()

            with sentry_sdk.start_transaction(name="test_end_to_end") as transaction:
                # ``DiscogsService.get_release`` is decorated with
                # ``@async_cached(RELEASE_CACHE)``, so its API-fetch closure is
                # private to the method body. Exercise the same seam shape
                # (``fallthrough()`` with the cache's ``get_release`` as
                # ``pg_read``) so the test reflects the real call shape without
                # punching through the decorator.
                from discogs.fallthrough import fallthrough

                async def call_seam(release_id: int) -> ReleaseMetadataResponse | None:
                    payload = api_payloads[release_id]

                    async def api_fetch() -> ReleaseMetadataResponse:
                        return payload

                    return await fallthrough(
                        label="get_release",
                        pg_read=lambda: cache_service.get_release(release_id),
                        api_fetch=api_fetch,
                        pg_write=None,  # write-back skipped for the test
                        is_pg_hit=lambda v: v is not None and v.artwork_url is not None,
                        breadcrumb_data={"release_id": release_id},
                    )

                # ----- Call 1: cache leg raises CacheUnavailableError, seam falls
                # through to the API payload, cool-down is armed.
                assert not _cool_down_active(), "Cool-down should be off at test start."
                first_result = await call_seam(101)

                # (1) API-fallback shape — same model the cache would have
                # returned, but populated by the API leg (cached=False on
                # an API result).
                assert first_result is not None
                assert first_result.release_id == 101
                assert first_result.cached is False

                # Cache leg was invoked exactly once for this call (the spy
                # records the L2 attempt that ultimately raised).
                assert cache_spy.call_count == 1

                # (2) Cool-down is armed.
                assert _cool_down_active(), (
                    "Cool-down should be armed after the cache leg raised "
                    "CacheUnavailableError under pool exhaustion."
                )

                # (4) Sentry transaction carries the cache-fallback payload.
                payload = transaction._data.get("cache_fallback_fired")
                assert payload is not None, (
                    "Expected `cache_fallback_fired` to be projected onto the "
                    f"transaction. Got data keys: {list(transaction._data.keys())}"
                )
                assert payload["reason"] == "get_release"
                assert payload["error_class"] == "CacheUnavailableError"
                assert payload["cool_down_seconds"] == _COOL_DOWN_SECONDS

                # ----- Call 2 (within cool-down window): cache leg must be
                # bypassed entirely; seam goes straight to API.
                second_result = await call_seam(102)
                assert second_result is not None
                assert second_result.release_id == 102

                # (3) Cache leg was NOT invoked on the second call — the
                # cool-down short-circuits ``pg_read``.
                assert cache_spy.call_count == 1, (
                    f"Expected cache leg to be skipped on second call; "
                    f"spy was invoked {cache_spy.call_count} times total."
                )
        finally:
            release.set()
            await hog
