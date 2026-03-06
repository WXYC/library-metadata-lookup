"""Integration test fixtures.

Provides a real LibraryDB backed by in-memory SQLite with FTS5,
seeded with representative catalog items.
"""

from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from config.settings import Settings
from discogs.models import DiscogsSearchResponse
from library.db import LibraryDB
from tests.factories import make_discogs_result

# ---------------------------------------------------------------------------
# Seed data -- representative catalog items
# ---------------------------------------------------------------------------

SEED_ITEMS = [
    (1, "A Night at the Opera", "Queen", "Q", 1, 1, "Rock", "CD"),
    (2, "The Game", "Queen", "Q", 1, 2, "Rock", "CD"),
    (3, "News of the World", "Queen", "Q", 1, 3, "Rock", "CD"),
    (4, "OK Computer", "Radiohead", "R", 1, 1, "Rock", "CD"),
    (5, "Kid A", "Radiohead", "R", 1, 2, "Electronic", "CD"),
    (6, "Dots and Loops", "Stereolab", "S", 1, 1, "Electronic", "CD"),
    (7, "Emperor Tomato Ketchup", "Stereolab", "S", 1, 2, "Electronic", "CD"),
    (8, "Vivadixiesubmarinetransmissionplot", "Sparklehorse", "S", 2, 1, "Rock", "CD"),
    (9, "Stankonia", "OutKast", "O", 1, 1, "Hip Hop", "CD"),
    (10, "Now That's What I Call Music 47", "Various Artists", "V", 1, 1, "Compilation", "CD"),
    (11, "Rock Classics", "Various Artists", "V", 1, 2, "Compilation", "CD"),
    (12, "Time (Clock of the Heart)", "Culture Club", "C", 1, 1, "Pop", "Vinyl"),
    (13, "Colour by Numbers", "Culture Club", "C", 1, 2, "Pop", "CD"),
    (14, "Living Colour", "Vivid", "L", 1, 1, "Rock", "CD"),  # note: intentionally swapped
    (15, "Vivid", "Living Colour", "L", 1, 1, "Rock", "CD"),
    (16, "Abbey Road", "The Beatles", "B", 1, 1, "Rock", "Vinyl"),
    (17, "Let It Be", "The Beatles", "B", 1, 2, "Rock", "Vinyl"),
    (18, "Laid Back", "Laid Back", "L", 2, 1, "Electronic", "CD"),
    (19, "Joni Mitchell", "Joni Mitchell", "MI", 8, 1, "Rock", "Vinyl"),
    (20, "Court and Spark", "Joni Mitchell", "MI", 8, 6, "Rock", "Vinyl"),
    (21, "S/t", "The Bird and the Bee", "BI", 125, 1, "Rock", "CD"),
    (22, "Please Clap Your Hands", "The Bird and the Bee", "BI", 125, 2, "Rock", "CD"),
]


async def _create_schema(conn: aiosqlite.Connection):
    """Create the library table and FTS5 virtual table."""
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
    """Insert seed catalog items and sync FTS index."""
    await conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        SEED_ITEMS,
    )
    # Rebuild FTS index from library table
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def library_db():
    """Real LibraryDB backed by in-memory SQLite with FTS5 and seed data."""
    db = LibraryDB()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await _create_schema(conn)
    await _seed_data(conn)

    # Bypass connect() path-checking by directly setting the connection
    db._conn = conn

    yield db

    await conn.close()


@pytest.fixture
def test_settings():
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
async def app_client(library_db, test_settings):
    """httpx AsyncClient with real LibraryDB but mocked Discogs/PostHog."""
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def app_client_with_discogs(library_db, test_settings):
    """httpx AsyncClient with real LibraryDB and a mock DiscogsService.

    The mock DiscogsService provides realistic track validation behavior:
    - validate_track_on_release returns True/False based on album title
    - search returns Discogs results for known albums
    """
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    mock_discogs = AsyncMock()
    mock_discogs.cache_service = None

    # Track validation: "Help Me" is on "Court and Spark" but NOT "Joni Mitchell" (self-titled)
    async def validate_track(release_id, song, artist):
        # release_id 1001 = Court and Spark, 1002 = self-titled
        return release_id == 1001

    mock_discogs.validate_track_on_release = AsyncMock(side_effect=validate_track)

    # Search returns matching Discogs results for known albums
    court_and_spark = make_discogs_result(
        release_id=1001, album="Court and Spark", artist="Joni Mitchell"
    )
    joni_self_titled = make_discogs_result(
        release_id=1002, album="Joni Mitchell", artist="Joni Mitchell"
    )

    async def search_discogs(request):
        album = request.album if hasattr(request, "album") else ""
        if album and "court" in album.lower():
            return DiscogsSearchResponse(results=[court_and_spark])
        if album and "joni" in album.lower():
            return DiscogsSearchResponse(results=[joni_self_titled])
        return DiscogsSearchResponse(results=[])

    mock_discogs.search = AsyncMock(side_effect=search_discogs)

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
