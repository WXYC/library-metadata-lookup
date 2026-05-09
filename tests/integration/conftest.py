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
    # Stereolab: multi-album artist (3 albums)
    (1, "Aluminum Tunes", "Stereolab", "RO", 87, 1, "Rock", "CD"),
    (2, "Dots and Loops", "Stereolab", "RO", 87, 2, "Electronic", "CD"),
    (3, "Emperor Tomato Ketchup", "Stereolab", "RO", 87, 3, "Electronic", "CD"),
    # Jessica Pratt + Cat Power: multiple genres
    (4, "On Your Own Love Again", "Jessica Pratt", "RO", 112, 1, "Rock", "Vinyl"),
    (5, "Moon Pix", "Cat Power", "RO", 23, 1, "Rock", "CD"),
    # Duke Ellington & John Coltrane: artist name with "&" (2 albums)
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
    (
        7,
        "In a Sentimental Mood (Single)",
        "Duke Ellington & John Coltrane",
        "JA",
        7,
        2,
        "Jazz",
        "Vinyl",
    ),
    # Various Artists: compilation handling
    (10, "Now That's What I Call Music 47", "Various Artists", "V", 1, 1, "Compilation", "CD"),
    (11, "Rock Classics", "Various Artists", "V", 1, 2, "Compilation", "CD"),
    # Juana Molina: non-English artist name
    (12, "DOGA", "Juana Molina", "RO", 42, 1, "Rock", "CD"),
    (13, "Wed 21", "Juana Molina", "RO", 42, 2, "Rock", "CD"),
    # Preserved from original: intentionally swapped artist/title for regression tests
    (14, "Living Colour", "Vivid", "L", 1, 1, "Rock", "CD"),
    (15, "Vivid", "Living Colour", "L", 1, 1, "Rock", "CD"),
    # Preserved: self-titled artist/album edge case
    (18, "Laid Back", "Laid Back", "L", 2, 1, "Electronic", "CD"),
    (19, "Joni Mitchell", "Joni Mitchell", "MI", 8, 1, "Rock", "Vinyl"),
    (20, "Court and Spark", "Joni Mitchell", "MI", 8, 6, "Rock", "Vinyl"),
    # Preserved: "S/t" self-titled abbreviation
    (21, "S/t", "The Bird and the Bee", "BI", 125, 1, "Rock", "CD"),
    (22, "Please Clap Your Hands", "The Bird and the Bee", "BI", 125, 2, "Rock", "CD"),
    # Preserved: quoted Discogs artist name regression
    (23, "Weird Al Yankovic", "Weird Al Yankovic", "YA", 1, 1, "Rock", "Vinyl"),
    (24, "Dare to Be Stupid", "Weird Al Yankovic", "YA", 1, 2, "Rock", "Vinyl"),
    (25, "Even Worse", "Weird Al Yankovic", "YA", 1, 3, "Rock", "Vinyl"),
    # Preserved: track on both artist album and VA compilation
    (26, "London Zoo", "The Bug", "B", 2, 1, "Electronic", "CD"),
    (27, "Pressure", "The Bug", "B", 2, 2, "Electronic", "CD"),
    (28, "The Sound of Dub", "Various Artists - Reggae", "V", 2, 1, "Reggae", "CD"),
    # Preserved: Grimes cluster (multiple artists/albums with "Grimes" in them)
    (29, "Tiny Grimes", "Tiny Grimes", "G", 1, 1, "Jazz", "CD"),
    (30, "Report from Grimes Creek", "Rosalie Sorrels", "S", 3, 1, "Folk", "CD"),
    (31, "Grimes Golden", "Further", "F", 1, 1, "Rock", "CD"),
    (32, "Live at the Kerava Jazz Festival", "Henry Grimes", "G", 2, 1, "Jazz", "CD"),
    (33, "Spirits Aloft", "Henry Grimes", "G", 2, 2, "Jazz", "CD"),
    (34, "Visions", "Grimes", "G", 3, 1, "Rock", "CD"),
    (35, "Halfaxa", "Grimes", "G", 3, 2, "Rock", "Vinyl"),
    (36, "Art Angels", "Grimes", "G", 3, 3, "Rock", "Vinyl"),
    (37, "Miss Anthropocene", "Grimes", "G", 3, 4, "Rock", "CD"),
    # Additional WXYC example artists
    (38, "Edits", "Chuquimamani-Condori", "EL", 15, 1, "Electronic", "Vinyl"),
    (39, "Confield", "Autechre", "EL", 16, 1, "Electronic", "CD"),
    (40, "1st Class", "Large Professor", "HH", 1, 1, "Hiphop", "CD"),
    (41, "...Destroys The Space Invaders", "Prince Jammy", "RE", 1, 1, "Reggae", "Vinyl"),
    # Lee 'Scratch' Perry: cluster used by the cache-promotion regression test.
    # Five fallback albums with low-id slots, plus "Live at Maritime Hall" deeper
    # in the catalog -- mirrors the production layout where the song-bearing
    # album sits past the artist-only FTS5 top-N slice.
    (12663, "Chicken Scratch", "Lee 'Scratch' Perry", "Pe", 1, 1, "Reggae", "Vinyl"),
    (12664, 'Ooh! Wah! 12"', "Lee 'Scratch' Perry", "Pe", 1, 2, "Reggae", "Vinyl"),
    (12665, "History Mystery Prophecy", "Lee 'Scratch' Perry", "Pe", 1, 3, "Reggae", "Vinyl"),
    (12680, "Arkology", "Lee 'Scratch' Perry", "Pe", 1, 17, "Reggae", "CD"),
    (12684, "Dub Fire", "Lee 'Scratch' Perry", "Pe", 1, 21, "Reggae", "CD"),
    (12682, "Live at Maritime Hall", "Lee 'Scratch' Perry", "Pe", 1, 20, "Reggae", "CD"),
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
    mock_discogs.get_release = AsyncMock(return_value=None)

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
