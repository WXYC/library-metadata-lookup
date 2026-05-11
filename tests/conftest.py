"""Shared test fixtures for pytest."""

from unittest.mock import AsyncMock, Mock

import pytest
from wxyc_fastapi.observability import RequestTelemetry

from discogs.service import DiscogsApiCheckResult
from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item


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
