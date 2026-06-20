"""E2E test fixtures.

Provides a fully-wired FastAPI app with a real in-memory SQLite library
and a mock Discogs service that returns realistic responses for WXYC
example artists.
"""

from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from config.settings import Settings
from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from library.db import LibraryDB
from tests.factories import make_discogs_result

# ---------------------------------------------------------------------------
# Seed data: WXYC example artists for full-pipeline E2E
# ---------------------------------------------------------------------------

E2E_SEED_ITEMS = [
    (1, "Aluminum Tunes", "Stereolab", "RO", 87, 1, "Rock", "CD"),
    (2, "Dots and Loops", "Stereolab", "RO", 87, 2, "Electronic", "CD"),
    (3, "DOGA", "Juana Molina", "RO", 42, 1, "Rock", "CD"),
    (4, "On Your Own Love Again", "Jessica Pratt", "RO", 112, 1, "Rock", "Vinyl"),
    (5, "Moon Pix", "Cat Power", "RO", 23, 1, "Rock", "CD"),
    (
        6,
        "Duke Ellington & John Coltrane",
        "Duke Ellington & John Coltrane",
        "JA",
        7,
        1,
        "Jazz",
        "CD",
    ),
    (7, "Edits", "Chuquimamani-Condori", "EL", 15, 1, "Electronic", "Vinyl"),
    (8, "Confield", "Autechre", "EL", 16, 1, "Electronic", "CD"),
    (9, "1st Class", "Large Professor", "HH", 1, 1, "Hiphop", "CD"),
    (10, "Freeform Compilation Vol. 1", "Various Artists", "V", 1, 1, "Compilation", "CD"),
]

# ---------------------------------------------------------------------------
# Discogs mock data
# ---------------------------------------------------------------------------

_DISCOGS_RELEASES = {
    "DOGA": ReleaseMetadataResponse(
        release_id=30001,
        title="DOGA",
        artist="Juana Molina",
        release_url="https://discogs.com/release/30001",
        tracklist=[
            TrackItem(position="1", title="la paradoja"),
            TrackItem(position="2", title="Eras"),
            TrackItem(position="3", title="Sin Dones"),
        ],
    ),
    "On Your Own Love Again": ReleaseMetadataResponse(
        release_id=30002,
        title="On Your Own Love Again",
        artist="Jessica Pratt",
        release_url="https://discogs.com/release/30002",
        tracklist=[
            TrackItem(position="1", title="Back, Baby"),
            TrackItem(position="2", title="Greycedes"),
            TrackItem(position="3", title="On Your Own Love Again"),
        ],
    ),
    "Aluminum Tunes": ReleaseMetadataResponse(
        release_id=30003,
        title="Aluminum Tunes",
        artist="Stereolab",
        release_url="https://discogs.com/release/30003",
        tracklist=[
            TrackItem(position="1", title="Spark Plug"),
            TrackItem(position="2", title="Simple Headphone Mind"),
        ],
    ),
}


def _make_mock_discogs_service():
    """Build a mock DiscogsService with realistic behavior for WXYC artists."""
    mock = AsyncMock()
    mock.cache_service = None

    # Search by album
    async def mock_search(request):
        album = getattr(request, "album", "") or ""
        artist = getattr(request, "artist", "") or ""
        album_lower = album.lower()
        artist_lower = artist.lower()

        if "doga" in album_lower:
            return DiscogsSearchResponse(
                results=[make_discogs_result(release_id=30001, album="DOGA", artist="Juana Molina")]
            )
        if "on your own" in album_lower or ("jessica" in artist_lower and "pratt" in artist_lower):
            return DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=30002,
                        album="On Your Own Love Again",
                        artist="Jessica Pratt",
                    )
                ]
            )
        if "aluminum" in album_lower or "stereolab" in artist_lower:
            return DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=30003, album="Aluminum Tunes", artist="Stereolab"
                    )
                ]
            )
        return DiscogsSearchResponse(results=[])

    mock.search = AsyncMock(side_effect=mock_search)

    # Get release metadata
    async def mock_get_release(release_id):
        for release in _DISCOGS_RELEASES.values():
            if release.release_id == release_id:
                return release
        return None

    mock.get_release = AsyncMock(side_effect=mock_get_release)

    # Track validation
    async def mock_validate(release_id, track, artist):
        release = await mock_get_release(release_id)
        if release is None:
            return False
        track_lower = track.lower()
        return any(
            track_lower in t.title.lower() or t.title.lower() in track_lower
            for t in release.tracklist
        )

    mock.validate_track_on_release = AsyncMock(side_effect=mock_validate)

    # Track search. Mirrors the real
    # ``search_releases_by_track(track, artist, limit, artist_as_keyword=...)``
    # signature: SONG_AS_TRACK, the ``lookup_releases_by_track`` helper (which
    # passes ``limit`` positionally), and SWAPPED_INTERPRETATION's narrowing
    # (via the shared kernel, LML#622) all call this with varying arg shapes.
    async def mock_search_by_track(track, artist=None, limit=20, **kwargs):
        track_lower = track.lower()
        if "paradoja" in track_lower:
            return TrackReleasesResponse(
                track="la paradoja",
                artist="Juana Molina",
                releases=[
                    ReleaseInfo(
                        album="DOGA",
                        artist="Juana Molina",
                        release_id=30001,
                        release_url="https://discogs.com/release/30001",
                    )
                ],
                total=1,
                cached=False,
            )
        if "back" in track_lower and "baby" in track_lower:
            return TrackReleasesResponse(
                track="Back, Baby",
                artist="Jessica Pratt",
                releases=[
                    ReleaseInfo(
                        album="On Your Own Love Again",
                        artist="Jessica Pratt",
                        release_id=30002,
                        release_url="https://discogs.com/release/30002",
                    )
                ],
                total=1,
                cached=False,
            )
        return TrackReleasesResponse(track=track, artist=artist, releases=[], total=0, cached=False)

    mock.search_releases_by_track = AsyncMock(side_effect=mock_search_by_track)

    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _create_schema(conn: aiosqlite.Connection):
    await conn.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist,
            content=library,
            content_rowid=id
        )
    """)
    await conn.commit()


async def _seed_data(conn: aiosqlite.Connection):
    await conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        E2E_SEED_ITEMS,
    )
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()


@pytest_asyncio.fixture
async def e2e_library_db():
    """Real LibraryDB backed by in-memory SQLite with E2E seed data."""
    db = LibraryDB()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await _create_schema(conn)
    await _seed_data(conn)
    db._conn = conn
    yield db
    await conn.close()


@pytest.fixture
def e2e_settings():
    """Settings with no real tokens, telemetry disabled."""
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest_asyncio.fixture
async def e2e_client(e2e_library_db, e2e_settings):
    """Full E2E client with real LibraryDB and mock Discogs returning realistic data."""
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    mock_discogs = _make_mock_discogs_service()

    app.dependency_overrides[get_library_db] = lambda: e2e_library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: e2e_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
