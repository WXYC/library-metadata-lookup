"""Unit tests for the album-title text-search fallback in search_compilations_for_track.

See WXYC/library-metadata-lookup#319 (sibling of #318, closes #237).

When the two parallel Discogs probes in ``search_compilations_for_track``
return no usable releases AND the request supplies an album title AND the
resolver pre-pass did not produce a high-confidence canonical swap, the
orchestrator retries with a title-only compilation search and routes the
candidate releases through the existing post-process loop. The trio-style
case from #237 (``Orcutt Shelley Miller`` / ``Orcutt Shelley Miller`` /
``A Star Is Born``) needs the existing ``album == artist`` skip in
``process_release`` to NOT fire for this code path, since the trio
intentionally has matching artist and album strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.orchestrator import CANONICAL_ARTIST_SIMILARITY_FLOOR, search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item


def _empty_response() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist=None, releases=[], total=0)


def _trio_response() -> TrackReleasesResponse:
    """Album-title fallback returns the Orcutt/Shelley/Miller release."""
    return TrackReleasesResponse(
        track="",
        artist=None,
        releases=[
            ReleaseInfo(
                album="Orcutt Shelley Miller",
                artist="Bill Orcutt, Corsano, Miller",
                release_id=34993109,
                release_url="https://www.discogs.com/release/34993109",
                is_compilation=False,
            )
        ],
        total=1,
    )


class TestAlbumTitleFallback:
    @pytest.mark.asyncio
    async def test_fallback_fires_when_results_empty_album_present_no_swap(self):
        """All three guard conditions met → ``search_releases_by_album_title`` runs."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_awaited_once()
        called_album = service.search_releases_by_album_title.await_args.args[0]
        assert called_album == "Orcutt Shelley Miller"

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_album_missing(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_resolver_swapped(self, monkeypatch):
        """If the resolver found a confident canonical, the existing probes
        already used it; the fallback adds no new information."""
        monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "true")
        from config.settings import get_settings

        get_settings.cache_clear()

        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "name": "Some Canonical Artist",
                    "score": CANONICAL_ARTIST_SIMILARITY_FLOOR + 0.10,
                }
            ]
        )
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())

        parsed = ParsedRequest(
            artist="Some Typoed Artist",
            album="An Album",
            song="A Song",
            raw_message="Some Typoed Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_not_called()
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_fallback_fires_after_initial_pass_filters_everything(self):
        """Staging regression from 2026-05-13 (#237 follow-up).

        Discogs has since added a canonical "Orcutt Shelley Miller" artist
        entity, so the artist-scoped probe in search_releases_by_track now
        DOES return the trio's 5 pressings. raw_releases is non-empty —
        which silenced the original `not raw_releases` fallback gate — and
        process_release rejects all 5 via the album==artist self-named
        guard (the library row's title 'Orcutt-Shelley-Miller' has an
        artist of 'Bill Orcutt', not the trio). End state: zero results.

        After the fix the gate fires on "no library results produced" so
        the fallback runs as a second pass with skip_self_named_album=False
        and skip_artist_match_filter=True, finds the library row, and
        validate_track_on_release confirms it.
        """
        # Library returns the trio album (matched by title — note the
        # hyphens in the actual library title) for any of these queries.
        library_item = make_library_item(
            id=72142,
            artist="Bill Orcutt",
            title="Orcutt-Shelley-Miller",
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[library_item])

        # Five pressings of the trio release returned by both probes.
        # is_compilation=False (Discogs lists this under format=Vinyl) and
        # album == parsed.artist, which trips the self-named-album guard
        # in the default process_release path.
        trio_pressings_response = TrackReleasesResponse(
            track="A Star Is Born",
            artist="Orcutt Shelley Miller",
            releases=[
                ReleaseInfo(
                    album="Orcutt Shelley Miller",
                    artist="Orcutt Shelley Miller",
                    release_id=rid,
                    release_url=f"https://www.discogs.com/release/{rid}",
                    is_compilation=False,
                )
                for rid in (35017901, 35220253, 34993109, 34998866, 34993607)
            ],
            total=5,
        )

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=trio_pressings_response)
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        service.search_releases_by_album_title.assert_awaited_once()
        assert any(r.id == 72142 for r in results), (
            "Expected library row 72142 to surface via the album-title fallback "
            "after the artist-scoped probes' candidates were all filtered out by "
            "process_release's default guards. Got: "
            f"{[(r.id, r.title) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_library_match_already_found(self):
        """The gate fires on ``not results`` (no library matches were
        produced), not ``not raw_releases``. (The gate name changed in #322;
        the prior incarnation tested the Discogs-response-empty condition,
        which no longer matches reality after Discogs added a canonical
        trio entity.) When the artist-scoped probes' candidates DO turn
        into a library match via the existing pipeline, the fallback adds
        no value and should not fire — its cost is one extra Discogs API
        call per fire."""
        library_item = make_library_item(id=42, artist="A Real Artist", title="An Album")
        db = AsyncMock()
        db.search = AsyncMock(return_value=[library_item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="A Song",
                artist="A Real Artist",
                releases=[
                    ReleaseInfo(
                        album="An Album",
                        artist="A Real Artist",
                        release_id=42,
                        release_url="https://www.discogs.com/release/42",
                    )
                ],
                total=1,
            )
        )
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="A Real Artist",
            album="An Album",
            song="A Song",
            raw_message="A Real Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _ = await search_compilations_for_track(db, parsed, discogs_service=service)

        assert any(r.id == 42 for r in results)
        service.search_releases_by_album_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_trio_release_surfaces_through_fallback_with_mismatching_library_artist(
        self,
    ):
        """The realistic trio case: the WXYC library row's artist string lists
        the trio members in catalog form (``"Orcutt, Bill / Shelley, Chris / Miller, Mette"``),
        which does NOT prefix-match the user-typed ``"Orcutt Shelley Miller"``.
        With ``skip_artist_match_filter=True`` on the fallback path, the library
        match should still reach ``validate_track_on_release`` (PR #236's fuzzy
        token-set-ratio validator), and the release should surface.
        """
        item = make_library_item(
            id=34993109,
            artist="Orcutt, Bill / Shelley, Chris / Miller, Mette",
            title="Orcutt Shelley Miller",
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        # validate_track_on_release stands in for PR #236's fuzzy validator —
        # the realistic trio credit ('Bill Orcutt, Corsano, Miller') passes via
        # rapidfuzz.token_set_ratio against parsed.artist.
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert any(r.id == 34993109 for r in results), (
            "Expected release 34993109 to surface via the fallback even when "
            "library artist string ('Orcutt, Bill / Shelley, Chris / Miller, "
            "Mette') does not prefix-match parsed.artist ('Orcutt Shelley Miller'). "
            "skip_artist_match_filter=True defers artist gating to "
            "validate_track_on_release."
        )
        service.validate_track_on_release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_release_still_rejected_when_validate_track_returns_false(self):
        """Even with skip_artist_match_filter=True, validate_track_on_release
        is the last line of defense — if the Discogs-side fuzzy validator says
        the track or artist doesn't match, the release does NOT surface."""
        item = make_library_item(
            id=34993109,
            artist="Orcutt, Bill / Shelley, Chris / Miller, Mette",
            title="Orcutt Shelley Miller",
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        service.validate_track_on_release = AsyncMock(return_value=False)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 34993109 for r in results)
