"""Unit tests for the A4 cached-track safety net (LML#629).

``find_library_albums_with_cached_track`` consults the local Discogs PG cache
("which releases by this artist contain this track?") after per-result track
validation confirmed nothing. Pre-#629 it returned only WXYC library rows whose
fuzzy match cleared ``artist_matches_item`` and *dropped* any cache-confirmed
release with no artist-matching row — losing a resolvable ``release_id`` the
cache already held.

A4 threads that ``release_id`` out: when the cache confirms the track on a
release but no library row artist-matches, the safety net surfaces the release
**row-less** (``LibraryItem(id=0)`` + ``{0: ResolvedRelease}`` on the
``discogs_titles`` seam) instead of returning empty — reusing #628's
carry-through shape. No-album ordering (the #629 rule) prefers the artist's own
release (``is_compilation=False``) over a compilation, then stable
``release_id``.
"""

from unittest.mock import AsyncMock

import pytest

from discogs.models import ReleaseInfo
from lookup.orchestrator import (
    ROWLESS_LIBRARY_ID,
    ROWLESS_NO_ALBUM_CONFIDENCE,
    find_library_albums_with_cached_track,
)
from lookup.release_resolution import ResolvedRelease


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    """Turn on LML_RESOLVE_NONLIBRARY_RELEASE so the A4 row-less carry-through fires."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def disable_nonlibrary_release(monkeypatch):
    """Force the flag off + reset the settings lru-cache so the flag-off assertion
    is self-contained, not dependent on a prior test's fixture teardown."""
    monkeypatch.delenv("LML_RESOLVE_NONLIBRARY_RELEASE", raising=False)
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cache_release(album, artist, release_id, *, is_compilation=False):
    return ReleaseInfo(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=is_compilation,
    )


@pytest.mark.asyncio
async def test_surfaces_rowless_release_id_when_no_library_row_matches(
    mock_library_db, mock_discogs_service, enable_nonlibrary_release
):
    """Cache confirms the track on a release the WXYC library doesn't carry →
    surface it row-less carrying the resolved ``release_id`` rather than dropping
    to empty."""
    mock_discogs_service.cache_service = AsyncMock()
    mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
        return_value=[_cache_release("Some Bootleg Compilation", "Lee Perry", 1000)]
    )
    mock_library_db.search.return_value = []

    items, discogs_titles = await find_library_albums_with_cached_track(
        mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
    )

    assert len(items) == 1
    assert items[0].id == ROWLESS_LIBRARY_ID
    resolved = discogs_titles[ROWLESS_LIBRARY_ID]
    assert isinstance(resolved, ResolvedRelease)
    assert resolved.release_id == 1000
    assert resolved.release_url == "https://www.discogs.com/release/1000"
    assert resolved.album_title == "Some Bootleg Compilation"


@pytest.mark.asyncio
async def test_prefers_own_release_over_compilation_when_no_library_row_matches(
    mock_library_db, mock_discogs_service, enable_nonlibrary_release
):
    """No-album ordering (#629): when the track sits on both the artist's own
    release and a compilation, surface the own release (``is_compilation=False``)
    even when the comp sorts first by ``release_id``."""
    mock_discogs_service.cache_service = AsyncMock()
    mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
        return_value=[
            _cache_release("Various: Reggae Hits", "Various", 1000, is_compilation=True),
            _cache_release("Roast Fish Collie Weed & Corn Bread", "Lee Perry", 2000),
        ]
    )
    mock_library_db.search.return_value = []

    items, discogs_titles = await find_library_albums_with_cached_track(
        mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
    )

    assert items[0].id == ROWLESS_LIBRARY_ID
    resolved = discogs_titles[ROWLESS_LIBRARY_ID]
    assert resolved.release_id == 2000
    assert resolved.is_compilation is False


@pytest.mark.asyncio
async def test_rowless_release_carries_soft_seam_confidence(
    mock_library_db, mock_discogs_service, enable_nonlibrary_release
):
    """The A4 pick is by track only — never album-matched — so it stamps the seam
    with a soft confidence. That soft value must ride the seam so the bind stays
    soft even when the request typed an album (the bind can't otherwise tell A4
    from an album-ranked carry-through)."""
    mock_discogs_service.cache_service = AsyncMock()
    mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
        return_value=[_cache_release("Some Bootleg Compilation", "Lee Perry", 1000)]
    )
    mock_library_db.search.return_value = []

    _, discogs_titles = await find_library_albums_with_cached_track(
        mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
    )

    assert discogs_titles[ROWLESS_LIBRARY_ID].confidence == ROWLESS_NO_ALBUM_CONFIDENCE


@pytest.mark.asyncio
async def test_drops_title_less_cache_release(
    mock_library_db, mock_discogs_service, enable_nonlibrary_release
):
    """A cache release with a valid release_id but no album title must not surface
    a degenerate row-less item (parity with the rehydrate-path empty-title guard)."""
    mock_discogs_service.cache_service = AsyncMock()
    mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
        return_value=[_cache_release("", "Lee Perry", 1000)]
    )
    mock_library_db.search.return_value = []

    items, discogs_titles = await find_library_albums_with_cached_track(
        mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
    )

    assert items == []
    assert discogs_titles == {}


@pytest.mark.asyncio
async def test_flag_off_preserves_drop_when_no_library_row_matches(
    mock_library_db, mock_discogs_service, disable_nonlibrary_release
):
    """Without ``lml_resolve_nonlibrary_release`` the A4 carry-through stays off:
    a cache-confirmed release with no artist-matching row drops to empty exactly
    as it did pre-#629 (fetch_artwork_for_items would not bind a row-less item)."""
    mock_discogs_service.cache_service = AsyncMock()
    mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
        return_value=[_cache_release("Some Bootleg Compilation", "Lee Perry", 1000)]
    )
    mock_library_db.search.return_value = []

    items, discogs_titles = await find_library_albums_with_cached_track(
        mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
    )

    assert items == []
    assert discogs_titles == {}
