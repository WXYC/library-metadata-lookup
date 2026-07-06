"""A2 no-album carry-through: artist+song (no typed album) resolvable non-library
tracks surface row-less, prefer the artist's own release over a compilation, and
carry a *soft* confidence (LML#629).

#628 already routes the no-album shapes (song-only via SONG_AS_TRACK, artist+song
via TRACK_ON_COMPILATION) through the row-less carry-through; #633 added the
bounded resolve whose ``prerank_candidates_for_validation`` applies the #629
no-album rule (prefer ``is_compilation=False``, then stable ``release_id``). This
suite pins that end-to-end through ``perform_lookup`` and adds the A2 soft
confidence: with no typed album the bound release is the best-ranked guess, so it
binds below the album-typed 1.0.

Driven against the real seeded ``library_db`` with a mock ``DiscogsService``.
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

ARTIST = "A Guy Called Gerald"
TRACK = "Message to Black Youth"

COMP_RELEASE_ID = 36907527
COMP_ALBUM = "When There Is No Sun"  # a Various-Artists compilation, not in library

OWN_RELEASE_ID = 12345
OWN_ALBUM = "Black Secret Technology"  # the artist's own LP, not in library


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _comp_release() -> ReleaseInfo:
    return ReleaseInfo(
        album=COMP_ALBUM,
        artist="Various",
        release_id=COMP_RELEASE_ID,
        release_url=f"https://www.discogs.com/release/{COMP_RELEASE_ID}",
        is_compilation=True,
    )


def _own_release() -> ReleaseInfo:
    return ReleaseInfo(
        album=OWN_ALBUM,
        artist=ARTIST,
        release_id=OWN_RELEASE_ID,
        release_url=f"https://www.discogs.com/release/{OWN_RELEASE_ID}",
        is_compilation=False,
    )


def _track_releases(*releases: ReleaseInfo) -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track=TRACK, artist=ARTIST, releases=list(releases), total=len(releases), cached=False
    )


def _empty_track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)


def _base_discogs_mock() -> AsyncMock:
    svc = AsyncMock()
    svc.cache_service = None
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.validate_track_on_release = AsyncMock(return_value=True)
    svc.search_releases_by_album_title = AsyncMock(return_value=_empty_track_releases())

    async def _get_release(release_id: int) -> ReleaseMetadataResponse:
        return ReleaseMetadataResponse(
            release_id=release_id,
            title=OWN_ALBUM if release_id == OWN_RELEASE_ID else COMP_ALBUM,
            artist=ARTIST if release_id == OWN_RELEASE_ID else "Various",
            release_url=f"https://www.discogs.com/release/{release_id}",
            artwork_url="https://i.discogs.com/cover.jpg",
        )

    svc.get_release = AsyncMock(side_effect=_get_release)
    return svc


async def _run(library_db, svc):
    """Drive perform_lookup with an artist+song (no typed album) request."""
    request = LookupRequest(artist=ARTIST, song=TRACK, raw_message=f"{ARTIST} - {TRACK}")
    return await perform_lookup(
        request, library_db, svc, telemetry=make_lml_telemetry(), discogs_cache_pg=None
    )


@pytest.mark.asyncio
async def test_artist_song_no_album_surfaces_rowless_with_soft_confidence(
    library_db, enable_nonlibrary_release
):
    """A resolvable non-library track with no typed album surfaces row-less with a
    real discogs_url, carrying the soft no-album confidence."""
    svc = _base_discogs_mock()
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_own_release()))

    response = await _run(library_db, svc)

    assert len(response.results) == 1
    item = response.results[0]
    assert item.library_item.id == 0
    assert item.artwork is not None
    assert item.artwork.release_id == OWN_RELEASE_ID
    assert item.artwork.release_url == f"https://www.discogs.com/release/{OWN_RELEASE_ID}"
    # A2: no typed album -> soft confidence (not the album-typed 1.0).
    assert item.artwork.confidence == ROWLESS_NO_ALBUM_CONFIDENCE


@pytest.mark.asyncio
async def test_no_album_prefers_own_album_over_compilation(library_db, enable_nonlibrary_release):
    """When the track sits on both the artist's own LP and a V/A compilation, the
    no-album ordering surfaces the own LP — even with the comp probed first."""
    svc = _base_discogs_mock()
    # Comp listed first to prove the prefer-own-release ordering flips it.
    svc.search_releases_by_track = AsyncMock(
        return_value=_track_releases(_comp_release(), _own_release())
    )

    response = await _run(library_db, svc)

    assert len(response.results) == 1
    item = response.results[0]
    assert item.library_item.id == 0
    assert item.artwork is not None
    assert item.artwork.release_id == OWN_RELEASE_ID
