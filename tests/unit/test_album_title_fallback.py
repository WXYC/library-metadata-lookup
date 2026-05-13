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
        """All three guard conditions met → ``search_compilations_by_title`` runs."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_compilations_by_title = AsyncMock(return_value=_empty_response())
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

        service.search_compilations_by_title.assert_awaited_once()
        called_album = service.search_compilations_by_title.await_args.args[0]
        assert called_album == "Orcutt Shelley Miller"

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_album_missing(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_compilations_by_title = AsyncMock(return_value=_empty_response())

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

        service.search_compilations_by_title.assert_not_called()

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
        service.search_compilations_by_title = AsyncMock(return_value=_empty_response())

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

        service.search_compilations_by_title.assert_not_called()
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_results_present(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        # First probe returns a non-empty result so the fallback shouldn't fire.
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
        service.search_compilations_by_title = AsyncMock(return_value=_empty_response())
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
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_compilations_by_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_trio_release_surfaces_through_fallback(self):
        """Self-named-album release (album == artist) returned by the fallback
        must NOT be skipped by ``process_release``'s ``album == artist`` guard."""
        item = make_library_item(
            id=34993109,
            artist="Orcutt Shelley Miller",
            title="Orcutt Shelley Miller",
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_compilations_by_title = AsyncMock(return_value=_trio_response())
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

        assert any(r.id == 34993109 for r in results)
