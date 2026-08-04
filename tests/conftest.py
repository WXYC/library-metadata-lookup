"""Shared test fixtures for pytest."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from wxyc_fastapi.observability import RequestTelemetry

from config.settings import get_settings
from discogs.service import DiscogsApiCheckResult
from lookup import streaming_url_postprocess as _streaming_mod
from lookup.streaming_url_postprocess import set_suppress_streaming_warm
from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item


@pytest.fixture(scope="session", autouse=True)
def scrub_posthog_env():
    """Blank ``POSTHOG_API_KEY`` for the whole pytest session (LML#879).

    The integration and e2e tiers' hermeticity convention is FastAPI
    ``dependency_overrides`` (``get_posthog_client -> None``), but the
    rate-gate fail-open emitter (``discogs/ratelimit._capture_fail_open``)
    bypasses DI by design — it reads ``get_posthog_client()`` (which builds a
    real singleton client straight from ``os.environ``) and the cached real
    ``Settings`` (telemetry default-on). Without this scrub, the first test
    that drives the gate into fail-open on a host whose shell or ``.env``
    carries a live key would send real ``discogs_rate_gate_fail_open`` events
    to the production PostHog project. E2E is the most exposed tier (it
    module-skips without a real ``DISCOGS_TOKEN``, so it runs precisely on
    hosts with a populated ``.env``); the unit tier is already covered by its
    own ``scrub_credential_env``, for which the extra blank here is a no-op.
    One session fixture in this root conftest rather than per-tier copies so
    the scrub can't drift between tiers. Blank rather than delete, mirroring
    ``tests/unit/conftest.py``'s ``scrub_credential_env``: env vars outrank
    the ``.env`` source, and empty behaves like unset throughout the codebase.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTHOG_API_KEY", "")
    get_settings.cache_clear()
    yield
    mp.undo()
    get_settings.cache_clear()


def reset_streaming_warm_state() -> None:
    """Reset the LML#706 streaming-warm module globals to a pristine state.

    The warm machinery is process-global by design (one bound per worker, not
    per request): the lazily-built semaphore, the dedup set, the strong-ref
    task set, and the bulk-suppression ContextVar. Any test module that
    touches the ``/lookup`` post-process should call this from an autouse
    fixture (setup AND teardown) so a leaked dedup key, a semaphore bound to
    a prior event loop, or a stuck suppression flag can't leak across tests.
    Single source of truth — the unit and integration suites previously
    carried diverging per-file copies of this reset.
    """
    _streaming_mod._streaming_warm_semaphore = None
    _streaming_mod._streaming_warm_concurrency = _streaming_mod._STREAMING_WARM_CONCURRENCY_DEFAULT
    _streaming_mod._streaming_warm_in_flight.clear()
    _streaming_mod._background_tasks.clear()
    set_suppress_streaming_warm(False)


async def drain_streaming_warm_tasks(timeout_s: float = 5.0) -> None:
    """Await every scheduled streaming-URL warm, bounded by ``timeout_s``.

    Loops because a task's done-callbacks (which discard it from the set and
    clear its dedup key) run after the ``gather`` returns, so one pass can
    observe a non-empty set. The deadline converts two regression shapes that
    would otherwise hang CI forever (the repo has no pytest-timeout and the
    workflow no ``timeout-minutes``) into an immediate, diagnosable failure:
    a warm that never completes blocks the ``gather`` until the deadline; a
    broken discard done-callback leaves completed tasks in the set and would
    busy-spin, which the per-iteration deadline check cuts off.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while _streaming_mod._background_tasks:
        remaining = deadline - loop.time()
        if remaining <= 0:
            pytest.fail(
                f"streaming-warm drain exceeded {timeout_s}s; "
                f"{len(_streaming_mod._background_tasks)} task(s) still tracked, "
                f"in-flight keys: {_streaming_mod._streaming_warm_in_flight}"
            )
        await asyncio.wait_for(
            asyncio.gather(*list(_streaming_mod._background_tasks), return_exceptions=True),
            timeout=remaining,
        )


@pytest.fixture(autouse=True)
def _reset_bulk_global_permit():
    """Suite-wide reset of the LML#716 process-global bulk-item permit.

    Sibling of ``_reset_lookup_inflight_cap`` below, same two failure modes:
    ``core.bulk_concurrency._bulk_global_semaphore`` is lazily built by the
    first bulk-family test in the session, which would (a) freeze that test's
    ``LML_BULK_GLOBAL_MAX_CONCURRENT`` snapshot for the rest of the run and
    (b) bind the semaphore to that test's event loop at the first contended
    acquire, surfacing as order-dependent ``RuntimeError(... bound to a
    different event loop)`` in unrelated files under pytest-asyncio's
    function-scoped loops.
    """
    from core import bulk_concurrency as _bulk_concurrency

    _bulk_concurrency._bulk_global_semaphore = None
    yield
    _bulk_concurrency._bulk_global_semaphore = None


@pytest.fixture(autouse=True)
def _reset_object_store_singleton():
    """Suite-wide reset of the WXYC#835 process-global object store.

    ``core.dependencies._object_store`` is a lazily built process-global
    (:class:`LocalDirStore` or :class:`S3ObjectStore`) cached on first
    ``get_object_store`` call. The admin streaming-DB endpoints (WXYC#836)
    resolve it, and each of their tests roots a fresh ``LocalDirStore`` at its
    own ``tmp_path`` (via an overridden ``get_settings``) or overrides the store
    outright for bucket mode. Left unreset, the FIRST such test's store would be
    reused by every later one — a stale ``tmp_path`` baseline read as another
    test's on-disk file, or a moto-backed S3 store leaking past its
    ``mock_aws`` context. Autouse at the root (not per-file) because both the
    unit guard suite and the integration round-trip touch it, and the reset is
    one attribute write. Nothing to close: neither store holds a persistent
    connection.
    """
    from core import dependencies as _core_deps

    _core_deps._object_store = None
    yield
    _core_deps._object_store = None


@pytest.fixture(autouse=True)
def _reset_lookup_inflight_cap():
    """Suite-wide reset of the LML#706 ``/lookup`` in-flight semaphore.

    ``lookup.router._lookup_semaphore`` is a process-global lazily built by
    the FIRST test in the session that POSTs ``/api/v1/lookup`` — whatever
    file that happens to live in. Left unreset, it (a) freezes that test's
    ``LML_LOOKUP_MAX_CONCURRENT`` snapshot for the rest of the session and
    (b) binds to that test's event loop at the first *contended* acquire,
    after which a contended acquire from a different pytest-asyncio
    function-scoped loop raises ``RuntimeError(... bound to a different event
    loop)`` — surfacing as order-dependent 500s in unrelated files. Autouse
    at the root (not per-file) because the exposure is every lookup-hitting
    test module, and the reset is one attribute write.
    """
    from lookup import router as _lookup_router

    _lookup_router._lookup_semaphore = None
    yield
    _lookup_router._lookup_semaphore = None


@pytest.fixture(autouse=True)
def _reset_streaming_check_inflight_cap():
    """Suite-wide reset of the LML#753 ``/streaming-check`` in-flight semaphore.

    Sibling of ``_reset_lookup_inflight_cap`` above, same two failure modes:
    ``streaming.router._streaming_check_semaphore`` is a process-global lazily
    built by the FIRST test in the session that POSTs ``/api/v1/streaming-check``,
    which would otherwise (a) freeze that test's
    ``LML_STREAMING_CHECK_MAX_CONCURRENT`` snapshot for the rest of the session
    and (b) bind to that test's event loop at the first contended acquire,
    surfacing as order-dependent ``RuntimeError(... bound to a different event
    loop)`` in unrelated files under pytest-asyncio's function-scoped loops.
    """
    from streaming import router as _streaming_router

    _streaming_router._streaming_check_semaphore = None
    yield
    _streaming_router._streaming_check_semaphore = None


@pytest.fixture(autouse=True)
def _reset_discogs_pool_singleton():
    """Suite-wide reset of the discogs-cache ``async_singleton`` lock (LML#706).

    Sibling of ``_reset_lookup_inflight_cap`` for the same failure mode, one
    layer deeper. ``async_singleton`` (``wxyc_fastapi``) builds a single
    ``asyncio.Lock`` at import (captured in the ``get_discogs_pool`` closure)
    and caches the pool instance; neither is reset by the closer. Under
    pytest-asyncio's function-scoped loops, a test whose loop is torn down
    while ``_build_discogs_pool`` is mid-acquire — the offline
    ``asyncpg.create_pool`` never connects to :5433, so the getter sits inside
    ``async with lock`` — leaves the lock ``[locked]`` and bound to a dead
    loop. The next test that resolves the pool (traced through
    ``get_discogs_cache_pg`` in the ``/lookup`` Depends chain) then raises
    ``RuntimeError(... bound to a different event loop)``, surfacing as an
    order-dependent failure in an unrelated file. Replace the shared ``lock``
    closure cell so every by-reference importer of the getter
    (``identity.dependencies``, ``main``) starts each test with a fresh,
    unbound lock.

    Only the lock is reset — the cached pool ``instance`` is left to the
    existing closers (``close_discogs_pool`` in the ``app_client`` /
    ``reset_globals`` teardowns). On the flake path ``instance`` is still
    ``None`` (the mid-acquire ``create_pool`` never returned), so nulling it
    here is unnecessary; doing it *without* ``aclose`` would instead orphan a
    live asyncpg pool in the pg suite (a connection leak), so we don't.
    """
    from core import dependencies as _core_deps

    def _reset() -> None:
        getter = _core_deps.get_discogs_pool
        freevars = getter.__code__.co_freevars
        # Fail loud if async_singleton's internals are renamed: a silent no-op
        # here would let the cross-loop flake return with zero test signal.
        assert "lock" in freevars, (
            "get_discogs_pool has no 'lock' free variable — wxyc_fastapi "
            "async_singleton internals changed; update this reset."
        )
        # Non-empty freevars => a real closure, so __closure__ is not None
        # (CPython invariant). The assert both narrows the type for mypy and
        # documents the invariant.
        closure = getter.__closure__
        assert closure is not None
        for name, cell in zip(freevars, closure, strict=True):
            if name == "lock":
                cell.cell_contents = asyncio.Lock()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_bio_warm_semaphore():
    """Suite-wide reset of the bio-warm ``_warm_cache_semaphore`` (LML#748).

    Sibling of ``reset_streaming_warm_state`` for the same failure mode:
    ``lookup.enrichment.background._warm_cache_semaphore`` is lazily bound to
    whichever event loop runs the first warm. Under pytest-asyncio's
    fresh-loop-per-test model, a later test's warm raises ``RuntimeError(...
    bound to a different event loop)``, which the warmer's blanket except
    swallows — warms silently no-op for the rest of the session and
    warm-side-effect assertions flake by test order.
    """
    from lookup.enrichment import background as _background_mod

    _background_mod._warm_cache_semaphore = None
    yield
    _background_mod._warm_cache_semaphore = None


def make_lml_telemetry() -> RequestTelemetry:
    """Build a `RequestTelemetry` with LML's production parameters.

    Single source of truth so the per-call kwargs in `lookup/router.py` and
    every test that constructs telemetry stay in sync.
    """
    return RequestTelemetry(
        api_call_keys=["discogs"],
        distinct_id="library-metadata-lookup-service",
        event_prefix="lookup",
    )


@pytest.fixture
def mock_library_db():
    """Create a mock library database."""
    db = AsyncMock()
    db.search = AsyncMock(return_value=[])
    db.exact_title = AsyncMock(return_value=[])
    db.find_similar_artist = AsyncMock(return_value=None)
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db.is_available = AsyncMock(return_value=True)
    db._conn = Mock()
    return db


@pytest.fixture
def mock_discogs_service():
    """Create a mock Discogs service."""
    service = AsyncMock()
    service.search = AsyncMock()
    service.validate_track_on_release = AsyncMock()
    service.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)
    # Explicit None (not an auto-AsyncMock): consumers treat "no release" as
    # a normal outcome, and an awaited auto-mock would leak a truthy Mock
    # into e.g. the V/A rescue's tracklist walk.
    service.get_release = AsyncMock(return_value=None)
    service.cache_service = None
    return service


@pytest.fixture
def mock_library_db_real():
    """Create a real LibraryDB instance with a mocked connection.

    Unlike mock_library_db which is a fully mocked AsyncMock, this creates
    a real LibraryDB so we can test internal methods like _fallback_like_search.
    """
    from library.db import LibraryDB

    db = LibraryDB(db_path=None)
    conn = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=cursor)
    db._conn = conn
    return db


@pytest.fixture
def sample_library_item():
    """Create a sample library item for testing."""
    return make_library_item(
        id=1,
        artist="Stereolab",
        title="Aluminum Tunes",
        call_letters="RO",
    )


@pytest.fixture
def sample_library_items():
    """Create multiple sample library items for testing."""
    return [
        make_library_item(
            id=1,
            artist="Stereolab",
            title="Aluminum Tunes",
            call_letters="RO",
        ),
        make_library_item(
            id=2,
            artist="Stereolab",
            title="Dots and Loops",
            call_letters="RO",
            release_call_number=2,
        ),
    ]


@pytest.fixture
def sample_parsed_request():
    """Create a sample parsed request for testing."""
    return ParsedRequest(
        song="la paradoja",
        album="DOGA",
        artist="Juana Molina",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Play la paradoja by Juana Molina",
    )
