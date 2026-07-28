"""Unit tests for the TRACK_ON_COMPILATION release-credit artist bind (LML#971).

Repro: artist="C. Spencer Yeh", song="In the Blink of an Eye" (no album)
returned no library results even though WXYC owns the release (library id
57833, artist "Burning Star Core" -- Yeh's band -- with alternate_artist_name
"C.S. Yeh"). Discogs credits the release (2789357) to "C.S. Yeh*", an Artist
Name Variation of the canonical "C. Spencer Yeh"; the trailing ``*`` is the
Discogs ANV marker ``DiscogsService._parse_title`` leaves in place.

``process_release`` found the row by title via ``search_album_fuzzy``, but
its strict artist filter only checked the typed artist ("C. Spencer Yeh")
against the row -- which prefix-matches neither the band name nor the alias
-- and the row isn't a V/A compilation, so the compilation carve-out couldn't
rescue it either. The row was silently dropped despite Discogs' own track
search returning this exact release for this exact artist.

Fix: the strict filter also accepts a title-matched row that prefix-matches
the release's OWN (ANV-stripped) Discogs credit. ``validate_release_for_track``
(run right after, keyed on the TYPED artist) remains the safety belt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item

_LIBRARY_ID = 57833
_RELEASE_ID = 2789357
_SONG = "In the Blink of an Eye"
_ARTIST = "C. Spencer Yeh"
_RELEASE_CREDIT = "C.S. Yeh*"
_RELEASE_ALBUM = "In The Blink Of An Eye"


def _burning_star_core_row() -> object:
    return make_library_item(
        id=_LIBRARY_ID,
        artist="Burning Star Core",
        alternate_artist_name="C.S. Yeh",
        title='"In The Blink of an Eye" 7-inch',
        genre="Rock",
    )


def _yeh_release_response(artist_credit: str = _RELEASE_CREDIT) -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track=_SONG,
        artist=artist_credit,
        releases=[
            ReleaseInfo(
                album=_RELEASE_ALBUM,
                artist=artist_credit,
                release_id=_RELEASE_ID,
                release_url=f"https://www.discogs.com/release/{_RELEASE_ID}",
                is_compilation=False,
            )
        ],
        total=1,
    )


def _service(artist_credit: str = _RELEASE_CREDIT) -> AsyncMock:
    """A DiscogsService double whose track search returns the C.S. Yeh* release
    and whose per-track validator passes -- the "Discogs side works" half of
    the bug (already confirmed live)."""
    service = AsyncMock()
    service.cache_service = None
    service.search_releases_by_track = AsyncMock(return_value=_yeh_release_response(artist_credit))
    service.validate_track_on_release = AsyncMock(return_value=True)
    return service


def _parsed() -> ParsedRequest:
    return ParsedRequest(
        artist=_ARTIST,
        song=_SONG,
        raw_message="In the Blink of an Eye by C. Spencer Yeh",
    )


@pytest.mark.asyncio
class TestReleaseCreditArtistBind:
    async def test_binds_library_row_via_release_credit_when_typed_artist_misses(self):
        """The core repro: the typed artist doesn't prefix-match the library
        row, but the release's own (ANV-marked) credit does -- the row must
        surface, carrying the validated release on the discogs_titles seam."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[_burning_star_core_row()])
        db.search = AsyncMock(return_value=[])

        service = _service()

        results, discogs_titles = await search_compilations_for_track(
            db, _parsed(), discogs_service=service
        )

        assert [r.id for r in results] == [_LIBRARY_ID]
        assert _LIBRARY_ID in discogs_titles
        assert discogs_titles[_LIBRARY_ID].release_id == _RELEASE_ID

        # The safety belt still runs, keyed on the TYPED artist -- not the
        # release credit -- so binding on the credit can't skip validation.
        service.validate_track_on_release.assert_awaited_once_with(_RELEASE_ID, _SONG, _ARTIST)

    async def test_failed_validation_still_drops_the_row(self):
        """The release-credit bind is not a bypass for validate_release_for_track:
        a title+credit match that fails per-track validation is still dropped."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[_burning_star_core_row()])
        db.search = AsyncMock(return_value=[])

        service = _service()
        service.validate_track_on_release = AsyncMock(return_value=False)

        results, discogs_titles = await search_compilations_for_track(
            db, _parsed(), discogs_service=service
        )

        assert results == []
        assert discogs_titles == {}

    async def test_row_matching_neither_typed_artist_nor_release_credit_stays_dropped(self):
        """Guard against over-admission: a title-matched row whose artist matches
        NEITHER the typed artist NOR the (stripped) release credit must still be
        dropped by the strict filter -- and never reach validation."""
        db = AsyncMock()
        unrelated_row = make_library_item(
            id=99999,
            artist="Some Unrelated Artist",
            title='"In The Blink of an Eye" 7-inch',
            genre="Rock",
        )
        db.exact_title = AsyncMock(return_value=[unrelated_row])
        db.search = AsyncMock(return_value=[])

        service = _service()

        results, discogs_titles = await search_compilations_for_track(
            db, _parsed(), discogs_service=service
        )

        assert results == []
        assert discogs_titles == {}
        service.validate_track_on_release.assert_not_awaited()


@pytest.mark.asyncio
class TestNumericSuffixIsNotAnAliasSignal:
    """A numeric-only Discogs disambiguator ('(N)', no ANV '*') must NOT open
    the release-credit match-back path -- unlike the ANV marker, it carries no
    "this credits a different canonical artist" signal, only "the Nth Discogs
    artist with this name". Regression coverage for a bug caught in review of
    the LML#971 fix itself: gating the new branch on "any decoration changed
    the string" (rather than the ANV marker specifically) let a plain
    numeric-suffixed credit prefix-match an unrelated library row.
    """

    async def test_numeric_suffix_credit_does_not_bind_unrelated_row(self):
        """ "Sun (2)" strips to "Sun", which prefix-matches library row
        "Sunburned Hand of the Man" -- an unrelated artist. The numeric suffix
        alone must not make that prefix match trustworthy."""
        db = AsyncMock()
        sunburned_row = make_library_item(
            id=42,
            artist="Sunburned Hand of the Man",
            title="Headdress",
        )
        db.exact_title = AsyncMock(return_value=[sunburned_row])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = None
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Headdress",
                artist="Sun (2)",
                releases=[
                    ReleaseInfo(
                        album="Headdress",
                        artist="Sun (2)",
                        release_id=556,
                        release_url="https://www.discogs.com/release/556",
                        is_compilation=False,
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Sun Araw",
            song="Headdress",
            raw_message="Sun Araw - Headdress",
        )

        results, discogs_titles = await search_compilations_for_track(
            db, parsed, discogs_service=service
        )

        assert results == []
        assert discogs_titles == {}

    async def test_numeric_suffix_credit_does_not_bypass_title_score_gate(self):
        """ "Various (2)" strips to "Various", which prefix-matches a library
        row filed under "Various Artists ..." -- but the release isn't flagged
        as a compilation, so the title-score carve-out never gets a chance to
        run. The numeric suffix must not open a side door around it."""
        db = AsyncMock()
        va_row = make_library_item(
            id=7001,
            artist="Various Artists - Rock - D",
            title="Totally Different Comp Title",
        )
        db.exact_title = AsyncMock(return_value=[va_row])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = None
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="No Way Back",
                artist="Various (2)",
                releases=[
                    ReleaseInfo(
                        album="Some Other Compilation",
                        artist="Various (2)",
                        release_id=8001,
                        release_url="https://www.discogs.com/release/8001",
                        is_compilation=False,
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        results, discogs_titles = await search_compilations_for_track(
            db, parsed, discogs_service=service
        )

        assert results == []
        assert discogs_titles == {}
