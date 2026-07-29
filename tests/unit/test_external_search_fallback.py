"""Unit tests for Step 7's dispatch policy + sentinel builder (LML#750).

``search_external_fallback`` is the artist>album>song dispatch precedence
extracted out of ``lookup/orchestrator.py``'s ``_step_external_cache_fallback``;
``build_external_catalog_item`` is the single construction site for the
``"(external)"`` sentinel ``LibraryCatalogItem`` Backend-Service string-matches
on, shared by Step 7 and the Step-3a synthesized-item response build.
"""

from unittest.mock import AsyncMock

import pytest

from lookup.external_search import build_external_catalog_item, search_external_fallback
from services.parser import MessageType, ParsedRequest


def _parsed(*, artist=None, album=None, song=None) -> ParsedRequest:
    return ParsedRequest(
        artist=artist,
        album=album,
        song=song,
        message_type=MessageType.REQUEST,
        is_request=True,
    )


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock()
    cache.search_artists_by_name = AsyncMock(return_value=[])
    cache.search_releases_by_title = AsyncMock(return_value=[])
    cache.search_tracks_by_title = AsyncMock(return_value=[])
    return cache


# ``mock_mb_pg`` is provided by tests/unit/conftest.py.


class TestSearchExternalFallback:
    @pytest.mark.asyncio
    async def test_artist_takes_precedence_over_album_and_song(
        self, mock_discogs_cache, mock_mb_pg
    ):
        mock_discogs_cache.search_artists_by_name.return_value = [
            {"id": 1, "name": "Sun Araw"},
        ]

        candidates, source = await search_external_fallback(
            _parsed(artist="Sun Arraw", album="On Patrol", song="On Patrol"),
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "discogs"
        assert candidates == [{"artist": "Sun Araw", "title": ""}]
        mock_discogs_cache.search_releases_by_title.assert_not_called()
        mock_discogs_cache.search_tracks_by_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_dispatches_when_no_artist_typed(self, mock_discogs_cache, mock_mb_pg):
        mock_discogs_cache.search_releases_by_title.return_value = [
            {"id": 1, "artist": "Sun Araw", "title": "On Patrol"},
        ]

        candidates, source = await search_external_fallback(
            _parsed(album="On Patrol"),
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "discogs"
        assert candidates == [{"artist": "Sun Araw", "title": "On Patrol"}]

    @pytest.mark.asyncio
    async def test_song_dispatches_when_no_artist_or_album_typed(
        self, mock_discogs_cache, mock_mb_pg
    ):
        mock_discogs_cache.search_tracks_by_title.return_value = [
            {"id": 1, "artist": "Sun Araw", "title": "On Patrol"},
        ]

        candidates, source = await search_external_fallback(
            _parsed(song="On Patrol"),
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "discogs"
        assert candidates == [{"artist": "Sun Araw", "title": "On Patrol"}]

    @pytest.mark.asyncio
    async def test_bare_raw_message_with_no_typed_field_skips_fallback(
        self, mock_discogs_cache, mock_mb_pg
    ):
        candidates, source = await search_external_fallback(
            _parsed(),
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert candidates == []
        assert source is None
        mock_discogs_cache.search_artists_by_name.assert_not_called()
        mock_discogs_cache.search_releases_by_title.assert_not_called()
        mock_discogs_cache.search_tracks_by_title.assert_not_called()


class TestBuildExternalCatalogItem:
    def test_builds_the_external_sentinel(self):
        item = build_external_catalog_item(artist="Sun Araw", title="On Patrol")

        assert item.id == 0
        assert item.artist == "Sun Araw"
        assert item.title == "On Patrol"
        assert item.call_number == "(external)"
        assert item.library_url == ""

    def test_blank_title_normalizes_to_none(self):
        item = build_external_catalog_item(artist="Sun Araw", title="")

        assert item.title is None
