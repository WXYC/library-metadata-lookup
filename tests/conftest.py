"""Shared test fixtures for pytest."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from wxyc_fastapi.observability import RequestTelemetry

from discogs.service import DiscogsApiCheckResult
from lookup import streaming_url_postprocess as _streaming_mod
from lookup.streaming_url_postprocess import set_suppress_streaming_warm
from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item


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
