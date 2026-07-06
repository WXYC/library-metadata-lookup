"""SONG_AS_ARTIST row-less carry-through (LML#631).

When a listener requests an artist WXYC doesn't own but who resolves cleanly on
Discogs, SONG_AS_ARTIST surfaces a *row-less* result (``LibraryItem(id=0)`` +
the resolved release on the ``discogs_titles`` seam) so request-o-matic can post
the Discogs context to Slack rather than "not found". Gated on the typed token
normalizing-equal to the resolved Discogs artist name and behind the shared
``LML_RESOLVE_NONLIBRARY_RELEASE`` flag (the binding side in
``fetch_artwork_for_items`` already binds any ``id=0`` item under that flag).

Driven end-to-end through ``perform_lookup`` against the real seeded
``library_db`` with a mock ``DiscogsService``: a ``song``-only request (no
artist/album) for an artist absent from the seed falls through the earlier
strategies to SONG_AS_ARTIST, which resolves the release via Discogs and emits
it row-less. Flag default-off asserts today's behavior (no row-less identity).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import (
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result

# A non-library artist that resolves on Discogs. Distinct from every seeded row.
SESSA_ARTIST = "Sessa"
SESSA_ALBUM = "Estrela Acesa"
SESSA_RELEASE_ID = 5551234
SESSA_RELEASE_URL = f"https://www.discogs.com/release/{SESSA_RELEASE_ID}"


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    """Turn on LML_RESOLVE_NONLIBRARY_RELEASE (shared with the #628 carry-through)."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _empty_track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)


def _base_mock() -> AsyncMock:
    """A mock DiscogsService where every track/compilation probe is empty, so a
    ``song``-only request falls through to SONG_AS_ARTIST. ``get_release`` backs
    the row-less artwork fallback (``_resolve_fallback_artwork``)."""
    svc = AsyncMock()
    svc.cache_service = None
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.search_releases_by_track = AsyncMock(return_value=_empty_track_releases())
    svc.search_releases_by_album_title = AsyncMock(return_value=_empty_track_releases())
    svc.validate_track_on_release = AsyncMock(return_value=False)
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=SESSA_RELEASE_ID,
            title=SESSA_ALBUM,
            artist=SESSA_ARTIST,
            release_url=SESSA_RELEASE_URL,
            artwork_url="https://i.discogs.com/sessa.jpg",
        )
    )
    return svc


def _patch_artist_releases():
    """Patch the artist-search seam so SONG_AS_ARTIST resolves the Sessa release."""
    return patch(
        "lookup.strategies.song_as_artist.lookup_releases_by_artist",
        new_callable=AsyncMock,
        return_value=[
            make_discogs_result(
                release_id=SESSA_RELEASE_ID,
                artist=SESSA_ARTIST,
                album=SESSA_ALBUM,
                release_url=SESSA_RELEASE_URL,
            )
        ],
    )


@pytest.mark.asyncio
async def test_song_as_artist_surfaces_rowless_when_flag_on(library_db, enable_nonlibrary_release):
    svc = _base_mock()
    request = LookupRequest(song=SESSA_ARTIST, raw_message=SESSA_ARTIST)

    with _patch_artist_releases():
        response = await perform_lookup(request, library_db, svc, telemetry=make_lml_telemetry())

    assert response.search_type == "song_as_artist"
    assert len(response.results) == 1
    item = response.results[0]
    assert item.library_item.id == 0
    assert item.artwork is not None
    assert item.artwork.release_id == SESSA_RELEASE_ID
    assert item.artwork.release_id > 0
    assert item.artwork.release_url == SESSA_RELEASE_URL


@pytest.mark.asyncio
async def test_song_as_artist_flag_off_no_rowless(library_db, monkeypatch):
    # Clear the lru-cached Settings at entry (not just rely on the flag-on
    # sibling's teardown) so a leaked flag=True cache can't make this exercise
    # flag-on behavior under reordering / parallel runs.
    monkeypatch.delenv("LML_RESOLVE_NONLIBRARY_RELEASE", raising=False)
    from config.settings import get_settings

    get_settings.cache_clear()

    svc = _base_mock()
    request = LookupRequest(song=SESSA_ARTIST, raw_message=SESSA_ARTIST)

    with _patch_artist_releases():
        response = await perform_lookup(request, library_db, svc, telemetry=make_lml_telemetry())

    get_settings.cache_clear()

    # Flag off: no row-less Discogs identity surfaces. The flag-on sibling
    # produces exactly one id=0 result (bound artwork) from these same inputs;
    # with the flag off the lookup falls through to the unresolved outcome, so
    # assert the concrete empty result set — a bare ``not any(...)`` would pass
    # vacuously on an empty list even if the gate were deleted or inverted.
    assert response.results == []
    assert response.search_type != "song_as_artist"


@pytest.mark.asyncio
async def test_song_as_artist_bulk_kill_switch_suppresses_rowless(
    library_db, enable_nonlibrary_release
):
    """LML#652: the #631 SONG_AS_ARTIST row-less surface respects the bulk kill
    switch. Flag on + ``allow_release_resolution_fallback=False`` (the /lookup/bulk
    drain) surfaces no row-less item — and, because no id==0 item reaches
    ``fetch_artwork_for_items``, fires no ``bind_carried`` artwork fetch (the
    row-less artwork fallback runs through ``get_release``, which must stay
    un-awaited). Contrast ``test_song_as_artist_surfaces_rowless_when_flag_on``,
    which surfaces the row-less item from these same inputs with the switch open.
    """
    svc = _base_mock()
    request = LookupRequest(song=SESSA_ARTIST, raw_message=SESSA_ARTIST)

    with _patch_artist_releases():
        response = await perform_lookup(
            request,
            library_db,
            svc,
            telemetry=make_lml_telemetry(),
            allow_release_resolution_fallback=False,
        )

    assert response.results == []
    assert response.search_type != "song_as_artist"
    svc.get_release.assert_not_awaited()
