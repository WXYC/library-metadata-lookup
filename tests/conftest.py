"""Shared test fixtures for pytest."""

from unittest.mock import AsyncMock, Mock

import pytest

from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item


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
    service.check_api = AsyncMock(return_value=True)
    service.cache_service = None
    return service


@pytest.fixture
def sample_library_item():
    """Create a sample library item for testing."""
    return make_library_item(
        id=1, artist="Queen", title="A Night at the Opera", call_letters="Q",
    )


@pytest.fixture
def sample_library_items():
    """Create multiple sample library items for testing."""
    return [
        make_library_item(
            id=1, artist="Queen", title="A Night at the Opera", call_letters="Q",
        ),
        make_library_item(
            id=2, artist="Queen", title="The Game",
            call_letters="Q", release_call_number=2,
        ),
    ]


@pytest.fixture
def sample_parsed_request():
    """Create a sample parsed request for testing."""
    return ParsedRequest(
        song="Bohemian Rhapsody",
        album="A Night at the Opera",
        artist="Queen",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Play Bohemian Rhapsody by Queen",
    )
