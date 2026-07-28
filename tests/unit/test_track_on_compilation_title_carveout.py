"""LML#973: tighten the TRACK_ON_COMPILATION title-ratio carve-out.

Root cause (LML#959): both the strict branch of ``process_release`` and its
``_fallback_row_acceptable`` fallback admit a Various-Artists library row into
the results purely on ``fuzz.ratio(release_album, library_title) >= 80``, even
though a title-ratio match alone can't tell "same comp, filed under a
shortened title" apart from "different comp, similarly worded title". Repro:
``lookup "i only have eyes for you by the flamingos"`` surfaces library row
58775 ("Various - Greatest hits of the 50s & 60s"), whose specific WXYC
pressing does not carry the track -- it was admitted because its title
fuzz-matches (ratio 87) a *different* Discogs compilation ("Greatest Hits Of
The 50's", release 605487) that Discogs validated the track on.

Fixture provenance:

- Library row 58775 and its title/artist, plus the validated Discogs release
  605487 ("Greatest Hits Of The 50's") and the query itself, are taken
  verbatim from the #959/#973 issue bodies (real prod data).
- Library rows 50963 and 8865 are cited in #973 by id only ("genuine comp
  hits for the same query") -- this sandbox has no access to prod
  library.db/discogs-cache to look up their real titles, so their titles and
  the Discogs releases they bind to below are SYNTHETIC placeholders
  constructed to plausibly satisfy "same query, genuinely carries the track".
  A human should confirm these two against the real library.db rows before
  treating them as a faithful regression pin (as opposed to just "some
  compilation row with an unrelated title survives the guard", which the
  divergent-title cases below already cover).
- The >=3 divergent-title recall-class cases (KEEP-A/B/E) are entirely
  SYNTHETIC -- invented pairs (Discogs comp title, abbreviated library title)
  chosen to plausibly represent the DJ card-catalog shorthand pattern
  (dropping a subtitle, a format tag, or a leading article) that a naive
  floor-raise would break. They are not sourced from any real WXYC row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item


def _empty_response() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist=None, releases=[], total=0)


def _track_search_mock(wave_a_response: TrackReleasesResponse) -> AsyncMock:
    """``search_releases_by_track`` double: Wave A (the ``limit=`` call) gets
    ``wave_a_response``; Wave B (``artist_as_keyword=True``) always gets an
    empty response. Keeps the merge in
    ``lookup.release_resolution.merge_wave_b_compilations`` out of these
    tests' way -- only Wave A's candidates matter here.
    """

    async def _search(*_args, **kwargs):
        if kwargs.get("artist_as_keyword"):
            return _empty_response()
        return wave_a_response

    return AsyncMock(side_effect=_search)


def _db_mock(by_query: dict[str, list]) -> AsyncMock:
    """``LibraryDB`` double: ``search`` routes on the exact ``query`` kwarg so
    each Discogs release's ``search_album_fuzzy(db, release_album)`` call in
    ``process_release`` only ever sees the library row it's meant to bind to
    (unmapped queries -- the keyword-search pre-pass, retry variants --
    return no rows).
    """

    async def _search(*, query: str, **_kwargs):
        return by_query.get(query, [])

    db = AsyncMock()
    db.exact_title = AsyncMock(return_value=[])
    db.search = AsyncMock(side_effect=_search)
    return db


def _service(releases: list[ReleaseInfo]) -> AsyncMock:
    service = AsyncMock()
    service.cache_service = AsyncMock()
    service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
    service.search_releases_by_track = _track_search_mock(
        TrackReleasesResponse(track="", artist=None, releases=releases, total=len(releases))
    )
    service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())
    # Every release below is, by construction, one Discogs already validated
    # the track on -- the bug is which *library row* that validated release
    # gets bound to, not whether validation itself passes.
    service.validate_track_on_release = AsyncMock(return_value=True)
    return service


@pytest.mark.asyncio
class TestStrictBranchTitleCarveout:
    """Covers ``process_release``'s strict branch (track_on_compilation.py
    around line 499) -- the site the #973 concrete repro hits: an
    artist+song query with no album, so ``album_fallback_should_fire`` is
    False and only the strict branch runs.
    """

    async def test_drops_wrong_pressing_58775(self):
        """Must-DROP: row 58775's title merely fuzz-matches the release
        Discogs validated the track on -- it is not that release."""
        item_58775 = make_library_item(
            id=58775,
            artist="Various Artists - Rock - G",
            title="Greatest hits of the 50s & 60s",
            format="vinyl",
        )
        release = ReleaseInfo(
            album="Greatest Hits Of The 50's",
            artist="Various",
            release_id=605487,
            release_url="https://www.discogs.com/release/605487",
            is_compilation=True,
        )
        db = _db_mock({"Greatest Hits Of The 50's": [item_58775]})
        service = _service([release])

        parsed = ParsedRequest(
            artist="The Flamingos",
            song="I Only Have Eyes for You",
            raw_message="I Only Have Eyes for You by The Flamingos",
        )

        with (
            patch(
                "lookup.strategies.track_on_compilation.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "lookup.strategies.track_on_compilation._resolve_nonlibrary_release",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 58775 for r in results), (
            "Library row 58775 ('Various - Greatest hits of the 50s & 60s') must "
            "not surface for 'I Only Have Eyes for You' by The Flamingos -- its "
            "title only fuzz-matches (ratio ~87) the Discogs release (605487, "
            "'Greatest Hits Of The 50's') that was actually validated to carry "
            f"the track; the two are different pressings. Got: "
            f"{[(r.id, r.title) for r in results]}"
        )

    async def test_keeps_genuine_same_query_hits_50963_and_8865(self):
        """Must-KEEP: two other library rows for the SAME query that
        genuinely bind to a validated release carrying the track.

        Ids 50963/8865 are named in #973; titles/releases here are
        synthetic placeholders (see module docstring) standing in for
        "another real pressing of a Flamingos 50s-doo-wop compilation".
        """
        item_58775 = make_library_item(
            id=58775,
            artist="Various Artists - Rock - G",
            title="Greatest hits of the 50s & 60s",
            format="vinyl",
        )
        item_50963 = make_library_item(
            id=50963,
            artist="Various Artists - Rock - S",
            title="Rockin' Fifties Doo Wop",
            format="vinyl",
        )
        item_8865 = make_library_item(
            id=8865,
            artist="Various Artists - Rock - D",
            title="Doo Wop Classics of the Fifties",
            format="CD",
        )
        releases = [
            ReleaseInfo(
                album="Greatest Hits Of The 50's",
                artist="Various",
                release_id=605487,
                release_url="https://www.discogs.com/release/605487",
                is_compilation=True,
            ),
            ReleaseInfo(
                album="Rockin' Fifties Doo Wop",
                artist="Various",
                release_id=111111,
                release_url="https://www.discogs.com/release/111111",
                is_compilation=True,
            ),
            ReleaseInfo(
                album="Doo Wop Classics of the Fifties",
                artist="Various",
                release_id=222222,
                release_url="https://www.discogs.com/release/222222",
                is_compilation=True,
            ),
        ]
        db = _db_mock(
            {
                "Greatest Hits Of The 50's": [item_58775],
                "Rockin' Fifties Doo Wop": [item_50963],
                "Doo Wop Classics of the Fifties": [item_8865],
            }
        )
        service = _service(releases)

        parsed = ParsedRequest(
            artist="The Flamingos",
            song="I Only Have Eyes for You",
            raw_message="I Only Have Eyes for You by The Flamingos",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        result_ids = {r.id for r in results}
        assert 50963 in result_ids, (
            f"Expected genuine comp hit 50963 to survive. Got: {[(r.id, r.title) for r in results]}"
        )
        assert 8865 in result_ids, (
            f"Expected genuine comp hit 8865 to survive. Got: {[(r.id, r.title) for r in results]}"
        )
        assert 58775 not in result_ids, "58775 must still be dropped in the combined query."

    @pytest.mark.parametrize(
        "release_album,library_title,library_id",
        [
            pytest.param(
                "Doo Wop's Greatest Hits, Vol. 1",
                "Doo Wops Greatest Hits Vol 1",
                61001,
                id="dropped-apostrophe-and-comma",
            ),
            pytest.param(
                "Soul Shots: Rare Southern Soul 45s",
                "Soul Shots Rare Southern Soul",
                61002,
                id="dropped-subtitle-tag",
            ),
            pytest.param(
                "The Best of Doo-Wop Uptempo",
                "Best of Doo Wop Uptempo",
                61003,
                id="dropped-leading-article-and-hyphen",
            ),
        ],
    )
    async def test_keeps_divergent_title_recall_class(
        self, release_album, library_title, library_id
    ):
        """Must-KEEP (recall-risk class): the library's card-catalog title
        differs from the Discogs release title's exact text (dropped
        punctuation, a subtitle tag, or a leading article) but the row
        genuinely carries the track -- the exact shape a naive floor-raise
        would break, since it only *omits* words from the Discogs title
        rather than asserting new ones.

        All three are entirely synthetic (see module docstring): invented
        titles, not sourced from a real WXYC row.
        """
        item = make_library_item(
            id=library_id,
            artist="Various Artists - Rock",
            title=library_title,
        )
        release = ReleaseInfo(
            album=release_album,
            artist="Various",
            release_id=90000 + library_id,
            release_url=f"https://www.discogs.com/release/{90000 + library_id}",
            is_compilation=True,
        )
        db = _db_mock({release_album: [item]})
        service = _service([release])

        parsed = ParsedRequest(
            artist="A Doo Wop Group",
            song="A Doo Wop Song",
            raw_message="A Doo Wop Song by A Doo Wop Group",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert any(r.id == library_id for r in results), (
            f"Expected divergent-title comp row {library_id} ('{library_title}', "
            f"Discogs title '{release_album}') to survive the tightened carve-out. "
            f"Got: {[(r.id, r.title) for r in results]}"
        )

    async def test_717_guard_intact_wrong_artist_non_compilation_row_still_rejected(self):
        """Regression guard for #717: a coincidental wrong-artist row on a
        NON-compilation release must still be rejected regardless of how
        close its title fuzz-matches -- the ``is_compilation_artist`` /
        ``discogs_is_compilation`` gate that protects #717 sits in front of
        (and is untouched by) the #973 title-scope guard.
        """
        wrong_artist_item = make_library_item(
            id=70229,
            artist="Galaxy 2 Galaxy",
            title="Galaxy Garden",
        )
        release = ReleaseInfo(
            album="Galaxy Garden",
            artist="Lone",
            release_id=4030652,
            release_url="https://www.discogs.com/release/4030652",
            is_compilation=False,
        )
        db = _db_mock({"Galaxy Garden": [wrong_artist_item]})
        service = _service([release])

        parsed = ParsedRequest(
            artist="Lone",
            song="Crystal Caverns 1991",
            raw_message="Lone - Crystal Caverns 1991",
        )

        with (
            patch(
                "lookup.strategies.track_on_compilation.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "lookup.strategies.track_on_compilation._resolve_nonlibrary_release",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 70229 for r in results), (
            "Wrong-artist non-compilation row 70229 must not surface for 'Lone' -- "
            f"reopens #717 if it does. Got: {[(r.id, r.title) for r in results]}"
        )


@pytest.mark.asyncio
class TestFallbackBranchTitleCarveout:
    """Covers the album-title fallback's ``_fallback_row_acceptable`` (around
    track_on_compilation.py line 519-529) -- the second carve-out site #973
    requires be handled consistently with the strict branch above.
    """

    async def test_drops_wrong_pressing_58775_via_fallback(self):
        """Same wrong-pressing collision as the strict-branch case, but
        reached through the album-title fallback (query supplies
        ``album=`` matching the Discogs comp exactly, driving Wave A/B to
        empty and the fallback to fire)."""
        item_58775 = make_library_item(
            id=58775,
            artist="Various Artists - Rock - G",
            title="Greatest hits of the 50s & 60s",
            format="vinyl",
        )
        db = _db_mock({"Greatest Hits Of The 50's": [item_58775]})

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(
                track="",
                artist=None,
                releases=[
                    ReleaseInfo(
                        album="Greatest Hits Of The 50's",
                        artist="Various",
                        release_id=605487,
                        release_url="https://www.discogs.com/release/605487",
                        is_compilation=True,
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="The Flamingos",
            album="Greatest Hits Of The 50's",
            song="I Only Have Eyes for You",
            raw_message="The Flamingos - I Only Have Eyes for You",
        )

        with (
            patch(
                "lookup.strategies.track_on_compilation.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "lookup.strategies.track_on_compilation._resolve_nonlibrary_release",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 58775 for r in results), (
            "Library row 58775 must not surface via the album-title fallback "
            f"either. Got: {[(r.id, r.title) for r in results]}"
        )
