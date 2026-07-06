"""A4 end-to-end: the cached-track safety net surfaces a non-library release
row-less through ``perform_lookup`` (LML#629).

The unit tests in ``tests/unit/test_cached_track_safety_net.py`` pin what
``find_library_albums_with_cached_track`` *returns*; they cannot see the caller
wiring. This drives the real pipeline down the Step-3b cached-track branch:

1. artist+song (no album) for the seeded ``Lee 'Scratch' Perry`` cluster — the
   artist-fallback surfaces real library rows (``real_results`` non-empty), so we
   enter the ``if real_results and parsed.song and parsed.artist`` block;
2. ``validate_track_on_release`` confirms nothing, so ``song_not_found`` holds and
   the ``elif song_not_found`` branch calls the cached-track safety net;
3. the PG cache confirms the track on a release the library does NOT carry, so no
   row artist-matches → A4 surfaces it row-less.

The load-bearing assertion is ``artwork.release_id > 0`` on the response: it
proves the resolved id survives the caller's ``discogs_titles`` merge
(``orchestrator.py`` Step-3b), the ``fetch_artwork_for_items`` bind, AND the
``enrich_artwork_results`` LML#487/#628 title-gate bypass — the chain that would
otherwise collapse to the BS#1185 ``release_id=0`` sentinel. None of that is
covered by the unit tests or by the A2 carry-through integration tests (which run
with ``cache_service=None``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.rowless import ROWLESS_NO_ALBUM_CONFIDENCE
from tests.conftest import make_lml_telemetry

ARTIST = "Lee 'Scratch' Perry"  # seeded cluster (six albums), no "Bucky Skank" row
SONG = "Bucky Skank"

# A real Lee Perry LP the seeded library does NOT carry, so the cache-confirmed
# release fuzzy-matches no library row and must surface row-less.
NONLIB_RELEASE_ID = 99999
NONLIB_ALBUM = "Roast Fish Collie Weed & Corn Bread"
NONLIB_RELEASE_URL = f"https://www.discogs.com/release/{NONLIB_RELEASE_ID}"


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def disable_nonlibrary_release(monkeypatch):
    """Force the flag off + reset the settings lru-cache so the flag-off assertion
    is self-contained, not dependent on a prior test's fixture teardown. Sets the
    env var to "false" rather than unsetting it so it also overrides a developer's
    local .env (Settings reads env_file=".env"; setenv takes precedence)."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "false")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cache_release() -> ReleaseInfo:
    return ReleaseInfo(
        album=NONLIB_ALBUM,
        artist=ARTIST,
        release_id=NONLIB_RELEASE_ID,
        release_url=NONLIB_RELEASE_URL,
        is_compilation=False,
    )


def _empty_track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)


def _service(*, with_cache_hit: bool) -> AsyncMock:
    """A mock service whose only track-confirmation channel is the PG cache.

    The live track probes return nothing (so the artist-fallback rows survive to
    Step-3b unvalidated), and the cache holds the non-library release.
    """
    svc = AsyncMock()
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.search_releases_by_track = AsyncMock(return_value=_empty_track_releases())
    svc.search_releases_by_album_title = AsyncMock(return_value=_empty_track_releases())
    # Nothing validates on the live path -> song_not_found holds into Step-3b.
    svc.validate_track_on_release = AsyncMock(return_value=False)
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=NONLIB_RELEASE_ID,
            title=NONLIB_ALBUM,
            artist=ARTIST,
            release_url=NONLIB_RELEASE_URL,
            artwork_url="https://i.discogs.com/roast.jpg",
        )
    )

    cache = AsyncMock()
    cache.search_releases_by_track = AsyncMock(
        return_value=[_cache_release()] if with_cache_hit else []
    )
    svc.cache_service = cache
    return svc


@pytest.fixture
def enable_nonlibrary_and_compilation(monkeypatch):
    """Both rollout flags on — the worst case for the A4 bulk kill switch (LML#652).

    ``LML_RESOLVE_COMPILATION_RELEASE`` makes ``fetch_artwork_for_items``'
    ``bind_carried`` *first* operand True for every item, so a row-less A4 item that
    leaked past the producer gate would still trust-bind (the second operand's new
    ``allow_release_resolution_fallback`` guard wouldn't help). Gating A4's producer
    itself is what closes the surface; this fixture proves it under both flags."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _run(library_db, svc, *, allow=True):
    request = LookupRequest(artist=ARTIST, song=SONG, raw_message=f"{ARTIST} - {SONG}")
    return await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=None,
        allow_release_resolution_fallback=allow,
    )


@pytest.mark.asyncio
async def test_cached_track_surfaces_rowless_release_end_to_end(
    library_db, enable_nonlibrary_release
):
    """The cache confirms the track on a non-library release; the safety net
    surfaces it row-less and the resolved release_id survives to the response."""
    svc = _service(with_cache_hit=True)

    response = await _run(library_db, svc)

    rowless = [r for r in response.results if r.library_item.id == 0 and r.artwork is not None]
    assert len(rowless) == 1
    item = rowless[0]
    # The chain held: caller merged the seam, fetch bound by id, enrich's #628
    # bypass kept the id off the sentinel.
    assert item.artwork.release_id == NONLIB_RELEASE_ID
    assert item.artwork.release_id > 0
    assert item.artwork.release_url == NONLIB_RELEASE_URL
    # No typed album -> A2 soft confidence on the A4 path too.
    assert item.artwork.confidence == ROWLESS_NO_ALBUM_CONFIDENCE


@pytest.mark.asyncio
async def test_cached_track_bulk_kill_switch_suppresses_rowless(
    library_db, enable_nonlibrary_and_compilation
):
    """LML#652: A4 is the fifth row-less producer — the issue enumerated only four
    and missed it (#629's A4 carry-through landed after the issue was pinned to the
    lagging prod tree). On /lookup/bulk (``allow_release_resolution_fallback=False``)
    it must surface no row-less item, even with ``LML_RESOLVE_COMPILATION_RELEASE``
    also on — which would otherwise let ``fetch_artwork_for_items``' ``bind_carried``
    first operand trust-bind a leaked id=0 item and fire a per-row ``get_release``
    artwork fetch. Contrast ``test_cached_track_surfaces_rowless_release_end_to_end``,
    which surfaces it from the same inputs with the switch open."""
    svc = _service(with_cache_hit=True)

    response = await _run(library_db, svc, allow=False)

    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )
    # No row-less item reached fetch_artwork_for_items, so no per-row artwork fetch
    # (the bind_carried get_release) fired for an id=0 item.
    svc.get_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_track_flag_off_no_rowless_release(library_db, disable_nonlibrary_release):
    """Flag off: the safety net stays inert — no row-less Discogs identity
    surfaces (pre-#629 drop preserved)."""
    svc = _service(with_cache_hit=True)

    response = await _run(library_db, svc)

    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )
