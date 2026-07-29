"""LML#801 end-to-end regression lock: an artist+track lookup surfaces the
in-library album that actually contains the track, instead of five unrelated
same-artist albums.

The incident: a Slack request ``Milkman Aphex Twin`` — "Milkman" is a track on
*Richard D. James Album*, which the WXYC library carries at a high catalog rowid
— returned the first five Aphex Twin releases by rowid and never surfaced the
album with the track. Root cause was #802: ``search_releases_by_track`` pruned
the artist's own release inside the ``matching_tracks`` CTE (``LIMIT`` before the
artist filter) for a common track title, so the #267 Step-3b cache rescue that
should have promoted *Richard D. James Album* got nothing back.

This test drives the **real** rescue path against a **real** seeded discogs-cache
(not a mocked ``search_releases_by_track`` — that would lock the orchestrator
wiring but not the CTE fix). The chain exercised is exactly the one #801 rests on:

1. artist-only fallback surfaces the wrong Aphex rows (rowid 80-84), ``song_not_found=True``;
2. ``filter_results_by_track_validation`` confirms none (live Discogs mocked empty);
3. the ``elif song_not_found`` branch calls ``find_library_albums_with_cached_track``,
   whose ``cache_service.search_releases_by_track("Milkman", "Aphex Twin", 20)`` hits
   the real cache — **with #802 it returns the RDJ release** despite 42 same-title
   decoys — then ``search_album_fuzzy``'s ``exact_title`` pre-pass maps it to the
   high-rowid library row and ``artist_matches_item`` promotes it.

Determinism mirrors ``test_search_by_track_artist_prefilter.py``: 42 decoy
releases (> ``limit*2 = 40``) carry a track titled exactly "Milkman" (sim 1.0);
the RDJ release's matching track is fuzzier ("Milkman (Reprise)") so it sorts
below all 42 and is reliably pruned on pre-#802 code. **This test is RED on
pre-#802 code** (the rescue gets ``[]`` → RDJ never promoted) and GREEN after.

Run with:
    pytest -m pg -v tests/integration/test_lookup_artist_track_recall.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.models import (
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from library.db import LibraryDB
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    skip_if_drop_targets_populated,
)

pytestmark = pytest.mark.pg

ARTIST = "Aphex Twin"
SONG = "Milkman"
RDJ_TITLE = "Richard D. James Album"
RDJ_LIBRARY_ID = 87  # beyond the top-5 Aphex rows (80-84) the rowid truncation keeps
RDJ_RELEASE_ID = 972  # discogs-cache release id

N_DECOYS = 42  # > limit*2 (= 40): floods the CTE LIMIT ahead of the fuzzier target
DECOY_TRACK = "Milkman"  # exact match -> sim 1.0
RDJ_TRACK = "Milkman (Reprise)"  # fuzzier -> sorts below the decoys (deterministic prune)

# Five other in-library Aphex releases at lower rowids than RDJ, so a bare-artist
# FTS search returns them first and the MAX_SEARCH_RESULTS=5 truncation drops RDJ.
_APHEX_LIBRARY_ROWS = [
    (80, "Selected Ambient Works Volume II", ARTIST),
    (81, "I Care Because You Do", ARTIST),
    (82, "Raising The Titanic", ARTIST),
    (83, "Donkey Rhubarb", ARTIST),
    (84, "Classics", ARTIST),
    (RDJ_LIBRARY_ID, RDJ_TITLE, ARTIST),
]


# ---------------------------------------------------------------------------
# Library (SQLite FTS5) — RDJ seeded at a high rowid
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def aphex_library_db():
    """In-memory LibraryDB seeded with six Aphex Twin rows, RDJ at rowid 87."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
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
            title, artist, content=library, content_rowid=id
        )
    """)
    await conn.executemany(
        "INSERT INTO library (id, title, artist, call_letters, artist_call_number, "
        "release_call_number, genre, format) VALUES (?, ?, ?, 'Ap', 2, ?, 'Electronic', 'CD')",
        [(rid, title, artist, rid - 79) for rid, title, artist in _APHEX_LIBRARY_ROWS],
    )
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()

    db = LibraryDB()
    db._conn = conn
    yield db
    await conn.close()


# ---------------------------------------------------------------------------
# discogs-cache (PostgreSQL) — real, so the CTE fix is genuinely exercised
# ---------------------------------------------------------------------------
async def _create_cache_schema(conn) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    await conn.execute(F_UNACCENT_WRAPPER_SQL)
    await conn.execute("DROP TABLE IF EXISTS release_track_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release CASCADE")
    await conn.execute("CREATE TABLE release (id integer PRIMARY KEY, title text NOT NULL)")
    await conn.execute(
        "CREATE TABLE release_track (release_id integer NOT NULL REFERENCES release(id) "
        "ON DELETE CASCADE, sequence integer NOT NULL, title text NOT NULL)"
    )
    await conn.execute(
        "CREATE TABLE release_artist (release_id integer NOT NULL REFERENCES release(id) "
        "ON DELETE CASCADE, artist_id integer, artist_name text NOT NULL, extra integer DEFAULT 0)"
    )
    await conn.execute(
        "CREATE TABLE release_track_artist (release_id integer NOT NULL REFERENCES release(id) "
        "ON DELETE CASCADE, track_sequence integer NOT NULL, artist_name text NOT NULL, "
        "extra integer DEFAULT 0)"
    )


async def _seed_decoys(conn) -> None:
    """42 releases by distinct decoy artists, each with a track titled exactly "Milkman"."""
    await conn.executemany(
        "INSERT INTO release (id, title) VALUES ($1, $2)",
        [(i, f"Decoy Album {i}") for i in range(1, N_DECOYS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, title) VALUES ($1, 1, $2)",
        [(i, DECOY_TRACK) for i in range(1, N_DECOYS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, 0)",
        [(i, f"Decoy Artist {i}") for i in range(1, N_DECOYS + 1)],
    )


async def _seed_rdj(conn) -> None:
    """The Aphex Twin release whose tracklist holds "Milkman" — the one that must
    survive the prune. Its matching track is fuzzier than the decoys so the
    pre-#802 CTE deterministically drops it."""
    await conn.execute("INSERT INTO release (id, title) VALUES ($1, $2)", RDJ_RELEASE_ID, RDJ_TITLE)
    await conn.execute(
        "INSERT INTO release_track (release_id, sequence, title) VALUES ($1, 10, $2)",
        RDJ_RELEASE_ID,
        RDJ_TRACK,
    )
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, 0)",
        RDJ_RELEASE_ID,
        ARTIST,
    )


@pytest_asyncio.fixture
async def cache_with_rdj(pg_pool):
    """Real discogs-cache holding 42 "Milkman" decoys + the Aphex RDJ release."""
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn, ("release", "release_track", "release_artist", "release_track_artist")
        )
        try:
            await _create_cache_schema(conn)
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")
        await _seed_decoys(conn)
        await _seed_rdj(conn)
    yield DiscogsCacheService(pg_pool)
    async with pg_pool.acquire() as conn:
        for t in ("release_track_artist", "release_track", "release_artist", "release"):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest_asyncio.fixture
async def cache_without_rdj(pg_pool):
    """Real discogs-cache holding only the decoys — the artist's tracklist is
    absent, so the #267 rescue must stay inert (no false promotion)."""
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn, ("release", "release_track", "release_artist", "release_track_artist")
        )
        try:
            await _create_cache_schema(conn)
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")
        await _seed_decoys(conn)
    yield DiscogsCacheService(pg_pool)
    async with pg_pool.acquire() as conn:
        for t in ("release_track_artist", "release_track", "release_artist", "release"):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


def _service(cache_service: DiscogsCacheService) -> AsyncMock:
    """A DiscogsService whose only track-confirmation channel is the real cache.

    Every live Discogs probe returns empty/False so the artist-fallback rows
    survive unvalidated into Step-3b and ``song_not_found`` holds — routing the
    request to ``find_library_albums_with_cached_track``, which reads the real
    ``cache_service``.
    """
    empty_releases = TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)
    svc = AsyncMock()
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.search_releases_by_track = AsyncMock(return_value=empty_releases)
    svc.search_releases_by_album_title = AsyncMock(return_value=empty_releases)
    svc.validate_track_on_release = AsyncMock(return_value=False)
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=RDJ_RELEASE_ID,
            title=RDJ_TITLE,
            artist=ARTIST,
            release_url=f"https://www.discogs.com/release/{RDJ_RELEASE_ID}",
        )
    )
    svc.cache_service = cache_service
    return svc


async def _run(library_db, svc):
    request = LookupRequest(artist=ARTIST, song=SONG, raw_message=f"{SONG} {ARTIST}")
    return await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=None,
    )


@pytest.mark.asyncio
async def test_milkman_aphex_twin_returns_rdj_end_to_end(aphex_library_db, cache_with_rdj):
    """The #801 incident, locked: {artist: Aphex Twin, song: Milkman, album: None}
    surfaces *Richard D. James Album* — the high-rowid in-library album the track
    is on — rather than the five unrelated same-artist albums. RED on pre-#802."""
    response = await _run(aphex_library_db, _service(cache_with_rdj))

    titles = [r.library_item.title for r in (response.results or [])]
    assert RDJ_TITLE in titles, (
        f"Expected '{RDJ_TITLE}' promoted from the cached-track rescue, got {titles}"
    )
    assert any(r.library_item.id == RDJ_LIBRARY_ID for r in (response.results or [])), (
        "RDJ should surface as its real high-rowid library row, not a row-less identity"
    )
    assert response.song_not_found is False


@pytest.mark.asyncio
async def test_cache_absent_leaves_rescue_inert(aphex_library_db, cache_without_rdj):
    """#267 invariant: when the cache does not hold the artist's tracklist, the
    rescue stays inert — it must not fabricate an RDJ promotion. song_not_found
    holds and no RDJ row is surfaced (the cache-absent behavior is unchanged)."""
    response = await _run(aphex_library_db, _service(cache_without_rdj))

    titles = [r.library_item.title for r in (response.results or [])]
    assert RDJ_TITLE not in titles
    assert response.song_not_found is True


def _service_with_live_track_validation(cache_service: DiscogsCacheService) -> AsyncMock:
    """LML#808: a DiscogsService whose live search+validate probes confirm the
    track on RDJ specifically — the API-backed path the widened artist-only
    fallback is meant to reach when the cache doesn't hold the release
    (``cache_service`` here is the cache-absent fixture, so the #267/#629
    cache rescue can't help; the *bounded* per-result validation must).
    """
    empty_releases = TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)

    async def _search(request):
        album = request.album or ""
        if album == RDJ_TITLE:
            return DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        release_id=RDJ_RELEASE_ID,
                        release_url=f"https://www.discogs.com/release/{RDJ_RELEASE_ID}",
                        album=RDJ_TITLE,
                        artist=ARTIST,
                    )
                ]
            )
        # The five lower-rowid Aphex albums: Discogs doesn't carry the track.
        return DiscogsSearchResponse(results=[])

    async def _validate(release_id, song, artist):
        return release_id == RDJ_RELEASE_ID

    svc = AsyncMock()
    svc.search = AsyncMock(side_effect=_search)
    svc.search_releases_by_track = AsyncMock(return_value=empty_releases)
    svc.search_releases_by_album_title = AsyncMock(return_value=empty_releases)
    svc.validate_track_on_release = AsyncMock(side_effect=_validate)
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=RDJ_RELEASE_ID,
            title=RDJ_TITLE,
            artist=ARTIST,
            release_url=f"https://www.discogs.com/release/{RDJ_RELEASE_ID}",
        )
    )
    svc.cache_service = cache_service
    return svc


@pytest.mark.asyncio
async def test_cache_miss_surfaces_rdj_via_bounded_api_validation(
    aphex_library_db, cache_without_rdj
):
    """LML#808: with RDJ absent from the cache (so the #267/#629 cache rescue
    can't help — see ``test_cache_absent_leaves_rescue_inert`` above), the
    widened artist-only fallback still reaches RDJ in
    ``filter_results_by_track_validation`` (past the old
    ``MAX_SEARCH_RESULTS``-by-rowid truncation) and the *bounded* fan-out
    confirms it via a live Discogs probe. RED before LML#808 (RDJ never
    reaches validation, only the five lower-rowid rows do); GREEN after."""
    response = await _run(aphex_library_db, _service_with_live_track_validation(cache_without_rdj))

    titles = [r.library_item.title for r in (response.results or [])]
    assert RDJ_TITLE in titles, (
        f"Expected '{RDJ_TITLE}' confirmed via the bounded API validation path, got {titles}"
    )
    assert any(r.library_item.id == RDJ_LIBRARY_ID for r in (response.results or [])), (
        "RDJ should surface as its real high-rowid library row, not a row-less identity"
    )
    assert response.song_not_found is False
