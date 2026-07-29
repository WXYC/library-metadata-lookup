"""LML#973 — tighten the TRACK_ON_COMPILATION title-ratio carve-out (option 1 of #959).

Repro of ``lookup "i only have eyes for you by the flamingos"``: library row
58775 ("Various - Greatest hits of the 50s & 60s") was surfaced and validated
against Discogs release 605487 ("Greatest Hits Of The 50's") — a *different*
comp than the one WXYC actually holds — because the compilation carve-out
admitted the row on ``fuzz.ratio(...) >= 80`` alone (87.3 for this pair). That
floor can't tell "same comp, reformatted title" from "different comp,
similarly-worded title": the extra "& 60s" is real content the Discogs release
doesn't share.

Fix: require the two titles to also be length-comparable
(``min(len)/max(len) >= _COMPILATION_TITLE_LENGTH_RATIO_FLOOR``) before the
carve-out admits a row. A title that's meaningfully longer or shorter than the
Discogs release it's being compared to carries content the other one lacks
entirely, which the ratio floor alone doesn't catch. Genuine reformattings
(punctuation, capitalization, an added "!", "Vol." vs "Vol") stay
length-comparable and keep clearing the floor.

Both carve-out sites in ``track_on_compilation.py`` apply the same two-part
test: the strict branch inside ``process_release`` (:~555) and the album-title
fallback's ``_fallback_row_acceptable`` (:~591).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item

_FLAMINGOS_PARSED = ParsedRequest(
    artist="The Flamingos",
    song="I Only Have Eyes for You",
    raw_message="i only have eyes for you by the flamingos",
)

# The wrong-pressing case from #973/#959: same query, a title-ratio-admitted
# row whose specific WXYC pressing lacks the track.
_WRONG_PRESSING_LIBRARY_ID = 58775
_WRONG_PRESSING_LIBRARY_TITLE = "Greatest hits of the 50s & 60s"
_WRONG_PRESSING_RELEASE_ID = 605487
_WRONG_PRESSING_RELEASE_ALBUM = "Greatest Hits Of The 50's"


def _release(release_id: int, album: str) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist="Various",
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=True,
    )


def _va_row(id: int, title: str) -> object:
    return make_library_item(id=id, artist="Various", title=title, format="vinyl")


def _service(releases: list[ReleaseInfo]) -> AsyncMock:
    service = AsyncMock()
    service.cache_service = AsyncMock()
    service.search_releases_by_track = AsyncMock(
        return_value=TrackReleasesResponse(
            track="I Only Have Eyes for You",
            artist="The Flamingos",
            releases=releases,
            total=len(releases),
        )
    )
    return service


async def _run(releases: list[ReleaseInfo], matches_by_album: dict[str, list[object]]):
    service = _service(releases)
    db = AsyncMock()
    db.search = AsyncMock(return_value=[])

    async def _fuzzy_side_effect(_db, album, *args, **kwargs):
        return matches_by_album.get(album, [])

    with (
        patch(
            "lookup.strategies.track_on_compilation.search_album_fuzzy",
            side_effect=_fuzzy_side_effect,
        ),
        patch(
            "lookup.strategies.track_on_compilation.validate_release_for_track",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        return await search_compilations_for_track(db, _FLAMINGOS_PARSED, discogs_service=service)


@pytest.mark.asyncio
class TestStrictBranchTitleRatioCarve:
    """The strict branch inside ``process_release`` (album+song, no album-title
    fallback — the path the concrete #973 repro hits)."""

    async def test_wrong_pressing_row_is_dropped(self):
        """Row 58775 must NOT surface: its title only clears the bare ratio
        floor (87.3), not the length-comparability guard (0.83)."""
        releases = [_release(_WRONG_PRESSING_RELEASE_ID, _WRONG_PRESSING_RELEASE_ALBUM)]
        matches_by_album = {
            _WRONG_PRESSING_RELEASE_ALBUM: [
                _va_row(_WRONG_PRESSING_LIBRARY_ID, _WRONG_PRESSING_LIBRARY_TITLE)
            ]
        }

        results, _titles = await _run(releases, matches_by_album)

        assert results == []

    async def test_genuine_same_titled_comps_are_kept(self):
        """Rows 50963 and 8865: genuine hits whose library title matches the
        validated Discogs release exactly — must still surface."""
        releases = [
            _release(50001, "Doo Wop Classics"),
            _release(50002, "Rock and Roll Party"),
        ]
        matches_by_album = {
            "Doo Wop Classics": [_va_row(50963, "Doo Wop Classics")],
            "Rock and Roll Party": [_va_row(8865, "Rock and Roll Party")],
        }

        results, _titles = await _run(releases, matches_by_album)

        assert {r.id for r in results} == {50963, 8865}

    async def test_divergent_but_length_comparable_titles_are_kept(self):
        """Recall-risk class: the WXYC catalog title differs from the Discogs
        title (punctuation, abbreviation, an added exclamation point) but the
        two are still length-comparable — a naive floor-raise would have no
        trouble here, but a length-delta guard must not overreach and drop
        these too."""
        releases = [
            _release(60001, "Estrus Fuzz Explosion"),
            _release(60002, "Nuggets Vol. 2"),
            _release(60003, "Bloodstains Across The Midwest"),
        ]
        matches_by_album = {
            "Estrus Fuzz Explosion": [_va_row(60101, "Estrus Fuzz Explosion!")],
            "Nuggets Vol. 2": [_va_row(60102, "Nuggets Vol 2")],
            "Bloodstains Across The Midwest": [_va_row(60103, "Bloodstains Across the Midwest!")],
        }

        results, _titles = await _run(releases, matches_by_album)

        assert {r.id for r in results} == {60101, 60102, 60103}

    async def test_717_wrong_artist_non_comp_collision_still_rejected(self):
        """LML#717 guard intact: a coincidental wrong-artist row (not filed
        under a compilation-artist name) is rejected regardless of title
        score — the carve-out never applies to it in the first place."""
        releases = [_release(70001, "Galaxy 2 Galaxy")]
        wrong_artist_row = make_library_item(
            id=70101, artist="Galaxy 2 Galaxy", title="Galaxy 2 Galaxy", format="vinyl"
        )
        matches_by_album = {"Galaxy 2 Galaxy": [wrong_artist_row]}

        results, _titles = await _run(releases, matches_by_album)

        assert results == []


@pytest.mark.asyncio
class TestFallbackBranchTitleRatioCarveParity:
    """The album-title fallback's ``_fallback_row_acceptable`` applies the same
    two-part test, reached when the artist-scoped probes surface nothing and
    ``parsed.album`` is supplied."""

    async def _run_fallback(self, album_fallback_releases: list[ReleaseInfo], matches: list):
        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(track="", artist=None, releases=[], total=0)
        )
        service.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(
                track="",
                artist=None,
                releases=album_fallback_releases,
                total=len(album_fallback_releases),
            )
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        parsed = ParsedRequest(
            artist="The Flamingos",
            album="Greatest Hits Of The 50's",
            song="I Only Have Eyes for You",
            raw_message="I Only Have Eyes for You - The Flamingos - Greatest Hits Of The 50's",
        )

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=matches,
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            return await search_compilations_for_track(db, parsed, discogs_service=service)

    async def test_wrong_pressing_row_dropped_via_fallback(self):
        releases = [_release(_WRONG_PRESSING_RELEASE_ID, _WRONG_PRESSING_RELEASE_ALBUM)]
        matches = [_va_row(_WRONG_PRESSING_LIBRARY_ID, _WRONG_PRESSING_LIBRARY_TITLE)]

        results, _titles = await self._run_fallback(releases, matches)

        assert results == []

    async def test_genuine_same_titled_comp_kept_via_fallback(self):
        releases = [_release(50001, "Doo Wop Classics")]
        matches = [_va_row(50963, "Doo Wop Classics")]

        results, _titles = await self._run_fallback(releases, matches)

        assert {r.id for r in results} == {50963}
