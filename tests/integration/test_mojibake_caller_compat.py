"""Integration tests for LML caller compatibility with V012-affected names.

Background: tubafrenzy MySQL is being repaired by V012 (Greek/Cyrillic/Arabic/CJK/
Latin Extended mojibake → corrected UTF-8). After V012 propagates through
discogs-etl/sync-library.sh into library.db, library rows will store the
*corrected* artist names. Every LML caller (Backend-Service, tubafrenzy, archive,
semantic-index, request-o-matic) sends names that have been read from one of:

    (a) tubafrenzy MySQL post-V012 → corrected form;
    (b) Backend-Service PG flowsheet (still mojibake until M2.1) → corrupted form;
    (c) user free-text → ASCII fallback ("u-Ziq", "Stella", "Nilufer Yanya");
    (d) hardcoded literal in caller source (none found in audit, but defensive).

These tests pin the contract that `/api/v1/lookup` finds the right release for
each of those input shapes when the library row is stored in the V012-corrected
form. They drive a fresh in-memory library through the real FastAPI stack with
mocked Discogs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.models import DiscogsSearchResponse
from library.db import LibraryDB
from main import app

# ---------------------------------------------------------------------------
# V012-affected fixtures: rows seeded in the *corrected* form (post-V012).
# ---------------------------------------------------------------------------

# (id, title, artist, call_letters, artist_call_number, release_call_number, genre, format)
# Artist forms intentionally use the codepoints they would carry post-V012.
# `µ-Ziq` mirrors the production library (MICRO SIGN U+00B5); the others use
# the natural-language Unicode codepoints.
MOJIBAKE_FIXTURE = [
    (1, "Lunatic Harness", "µ-Ziq [mu-Ziq]", "Mu", 3, 3, "Hiphop", "CD"),
    (2, "Up the Bracket", "Νilüfer Yanya", "Ny", 1, 1, "Rock", "CD"),
    (3, "Σtella Sings", "Σtella", "Si", 1, 1, "Pop", "CD"),
    (4, "Drápur", "Hermanos Gutiérrez", "He", 1, 1, "Rock", "CD"),
    (5, "Csillag", "Csillagrablók", "Cs", 1, 1, "Rock", "CD"),
]


async def _create_and_seed(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
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
        """
    )
    await conn.execute(
        """
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist,
            content=library,
            content_rowid=id
        )
        """
    )
    await conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        MOJIBAKE_FIXTURE,
    )
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()


@pytest_asyncio.fixture
async def mojibake_library_db():
    db = LibraryDB()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await _create_and_seed(conn)
    await db.adopt_connection(conn)
    yield db
    await conn.close()


@pytest.fixture
def mojibake_settings():
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest_asyncio.fixture
async def mojibake_client(mojibake_library_db, mojibake_settings):
    mock_discogs = AsyncMock()
    mock_discogs.cache_service = None
    mock_discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    mock_discogs.search_releases_by_track = AsyncMock(return_value=None)
    mock_discogs.get_release = AsyncMock(return_value=None)
    mock_discogs.validate_track_on_release = AsyncMock(return_value=False)

    app.dependency_overrides[get_library_db] = lambda: mojibake_library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: mojibake_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /lookup: V012-corrected query → V012-corrected library row should match.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artist", "album", "expected_artist_substring"),
    [
        ("µ-Ziq", "Lunatic Harness", "µ-Ziq"),
        ("μ-Ziq", "Lunatic Harness", "µ-Ziq"),  # Greek mu (U+03BC) → MICRO SIGN row
        ("Νilüfer Yanya", "Up the Bracket", "Yanya"),
        ("Σtella", "Σtella Sings", "Σtella"),
        ("Hermanos Gutiérrez", "Drápur", "Hermanos"),
        ("Csillagrablók", "Csillag", "Csillag"),
    ],
    ids=[
        "micro-sign-mu",
        "greek-mu",
        "greek-capital-nu",
        "greek-capital-sigma",
        "latin-extended-acute",
        "hungarian-ohungarumlaut",
    ],
)
async def test_lookup_finds_v012_corrected_row(
    mojibake_client, artist, album, expected_artist_substring
):
    response = await mojibake_client.post(
        "/api/v1/lookup",
        json={"artist": artist, "album": album, "raw_message": f"{artist} - {album}"},
    )

    assert response.status_code == 200
    payload = response.json()
    results = payload.get("results") or []
    assert results, (
        f"no result for {artist!r} / {album!r} (search_type={payload.get('search_type')})"
    )
    assert expected_artist_substring in results[0]["library_item"]["artist"]


# ---------------------------------------------------------------------------
# /lookup: ASCII / diacritic-stripped query should still find the row via the
# fuzzy fallback. This is the "user free-text" caller path (Slack, request-line).
#
# `ascii-hermanos` works because the library row's leading character `H` is
# Latin, so the per-result artist-match prefix check survives diacritic strip.
# `ascii-nilufer` is xfail: the row's leading char is Greek capital nu (Ν,
# U+039D) which has no NFKD decomposition, so after `normalize_for_comparison`
# the artist still starts with `ν` and the prefix match against `nilufer`
# fails. Documented in docs/audits/lml_capability_matrix.md as a real limitation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artist", "album", "expected_artist_substring"),
    [
        ("Hermanos Gutierrez", "Drapur", "Hermanos"),
        pytest.param(
            "Nilufer Yanya",
            "Up the Bracket",
            "Yanya",
            marks=pytest.mark.xfail(
                reason=(
                    "Greek capital nu (U+039D) survives NFKD; per-result artist-prefix "
                    "match in lookup/orchestrator.py:artist_matches_item rejects ASCII "
                    "queries against artists whose leading char is non-Latin."
                ),
                strict=True,
            ),
        ),
    ],
    ids=["ascii-hermanos", "ascii-nilufer"],
)
async def test_lookup_ascii_fallback_finds_diacritic_row(
    mojibake_client, artist, album, expected_artist_substring
):
    response = await mojibake_client.post(
        "/api/v1/lookup",
        json={"artist": artist, "album": album, "raw_message": f"{artist} - {album}"},
    )

    assert response.status_code == 200
    payload = response.json()
    results = payload.get("results") or []
    assert results, (
        f"ASCII fallback did not find {artist!r} / {album!r} "
        f"(search_type={payload.get('search_type')})"
    )
    assert expected_artist_substring in results[0]["library_item"]["artist"]


# ---------------------------------------------------------------------------
# /library/search: q= path runs the full normalization chain. This is the
# tubafrenzy autocomplete servlets' caller path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_artist_substring"),
    [
        ("μ-Ziq Lunatic", "µ-Ziq"),
        ("u-Ziq Lunatic", "µ-Ziq"),
        ("Σtella Sings", "Σtella"),
        pytest.param(
            "Stella Sings",
            "Σtella",
            marks=pytest.mark.xfail(
                reason=(
                    "Greek capital sigma (U+03A3) does not NFKD-decompose, then the "
                    "LIKE/fuzzy normalizer's `[^a-z0-9\\s]` regex strips it, so the "
                    "row's artist token becomes `tella` and ASCII `Stella` cannot "
                    "match the 3-char prefix candidate filter."
                ),
                strict=True,
            ),
        ),
    ],
    ids=["greek-mu-q", "ascii-q", "sigma-q", "ascii-sigma-q"],
)
async def test_library_search_q_finds_v012_row(mojibake_client, query, expected_artist_substring):
    response = await mojibake_client.get("/api/v1/library/search", params={"q": query, "limit": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1, f"q={query!r} returned 0 results"
    assert any(expected_artist_substring in r["artist"] for r in payload["results"]), (
        f"q={query!r} results did not include {expected_artist_substring!r}"
    )


# ---------------------------------------------------------------------------
# Pin the known asymmetry: /library/search?artist= is byte-strict and does NOT
# strip diacritics. Documented in docs/audits/lml_capability_matrix.md. If this
# behaviour ever changes, this test should fail and the matrix must be updated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_library_search_artist_filter_is_byte_strict(mojibake_client):
    # Library row stored as MICRO SIGN U+00B5; query uses GREEK SMALL LETTER MU U+03BC.
    response = await mojibake_client.get(
        "/api/v1/library/search", params={"artist": "μ-Ziq", "limit": 5}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0, (
        "artist= filter unexpectedly matched across codepoints. "
        "If LML now NFKC-normalizes this path, update "
        "docs/audits/lml_capability_matrix.md and remove the M0.4 caller warning."
    )
