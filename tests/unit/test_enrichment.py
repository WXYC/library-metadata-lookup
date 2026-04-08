"""Tests for metadata enrichment (release year, artist details, streaming links)."""

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ArtistDetails, DiscogsSearchResult, ReleaseMetadataResponse
from lookup.orchestrator import (
    _build_streaming_search_url,
    _fetch_apple_music_url,
    enrich_artwork_results,
)
from tests.factories import make_discogs_result, make_library_item


class TestBuildStreamingSearchUrl:
    def test_builds_encoded_url(self):
        url = _build_streaming_search_url(
            "https://open.spotify.com/search/", "Autechre", "VI Scose Poise"
        )
        assert url == "https://open.spotify.com/search/Autechre%20VI%20Scose%20Poise"

    def test_artist_only(self):
        url = _build_streaming_search_url("https://bandcamp.com/search?q=", "Juana Molina", "")
        assert "Juana%20Molina" in url

    def test_encodes_special_characters(self):
        url = _build_streaming_search_url(
            "https://soundcloud.com/search?q=", "Bj\u00f6rk", "J\u00f3ga"
        )
        assert "Bj%C3%B6rk" in url
        assert "J%C3%B3ga" in url


class TestFetchAppleMusicUrl:
    @pytest.mark.asyncio
    async def test_returns_url_on_success(self):
        import httpx

        mock_response = httpx.Response(
            200,
            json={"results": [{"trackViewUrl": "https://music.apple.com/us/album/test/123"}]},
            request=httpx.Request("GET", "https://itunes.apple.com/search"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await _fetch_apple_music_url(
            "Kate Bush", "The Saxophone Song", http_client=mock_client
        )
        assert result == "https://music.apple.com/us/album/test/123"

    @pytest.mark.asyncio
    async def test_returns_none_on_no_results(self):
        import httpx

        mock_response = httpx.Response(
            200,
            json={"results": []},
            request=httpx.Request("GET", "https://itunes.apple.com/search"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await _fetch_apple_music_url(
            "Obscure Artist", "Obscure Song", http_client=mock_client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))

        result = await _fetch_apple_music_url("Artist", "Song", http_client=mock_client)
        assert result is None


class TestEnrichArtworkResults:
    @pytest.mark.asyncio
    async def test_enriches_with_year_and_artist_details(self):
        item = make_library_item(artist="Kate Bush", title="The Kick Inside")
        artwork = make_discogs_result(
            release_id=10154369, artist="Kate Bush", album="The Kick Inside"
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=10154369,
            title="The Kick Inside",
            artist="Kate Bush",
            year=1978,
            artist_id=42,
            release_url="https://discogs.com/release/10154369",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Kate Bush",
            profile="English singer-songwriter.",
            urls=["https://en.wikipedia.org/wiki/Kate_Bush", "https://katebush.com"],
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                [(item, artwork)], discogs_service, song="The Saxophone Song"
            )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_year == 1978
        assert enriched.artist_bio == "English singer-songwriter."
        assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/Kate_Bush"
        assert "Kate+Bush" in enriched.spotify_url or "Kate%20Bush" in enriched.spotify_url
        assert enriched.youtube_music_url is not None
        assert enriched.bandcamp_url is not None
        assert enriched.soundcloud_url is not None

    @pytest.mark.asyncio
    async def test_handles_missing_artist_id(self):
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=123,
            title="Test",
            artist="Test",
            year=2020,
            artist_id=None,
            release_url="https://discogs.com/release/123",
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results([(item, artwork)], discogs_service)

        _, enriched = results[0]
        assert enriched.release_year == 2020
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_handles_none_artwork(self):
        item = make_library_item()

        results = await enrich_artwork_results([(item, None)], AsyncMock())

        _, enriched = results[0]
        assert enriched is None

    @pytest.mark.asyncio
    async def test_handles_no_discogs_service(self):
        item = make_library_item()
        artwork = make_discogs_result()

        results = await enrich_artwork_results([(item, artwork)], None)

        _, enriched = results[0]
        assert enriched is artwork  # unchanged

    @pytest.mark.asyncio
    async def test_handles_discogs_failure_gracefully(self):
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.side_effect = Exception("API error")

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results([(item, artwork)], discogs_service)

        _, enriched = results[0]
        # Enriched fields should be None but streaming URLs still generated
        assert enriched.release_year is None
        assert enriched.artist_bio is None
        assert enriched.spotify_url is not None  # search URL still works

    @pytest.mark.asyncio
    async def test_enriches_multiple_items_preserving_order(self):
        """Multiple items should all be enriched and returned in input order."""
        items_with_artwork = [
            (
                make_library_item(id=i, artist=f"Artist {i}", title=f"Album {i}"),
                make_discogs_result(release_id=i, artist=f"Artist {i}", album=f"Album {i}"),
            )
            for i in range(1, 4)
        ]

        discogs_service = AsyncMock()

        def make_release(release_id, **_kwargs):
            return ReleaseMetadataResponse(
                release_id=release_id,
                title=f"Album {release_id}",
                artist=f"Artist {release_id}",
                year=2000 + release_id,
                artist_id=None,
                release_url=f"https://discogs.com/release/{release_id}",
            )

        discogs_service.get_release.side_effect = lambda rid: make_release(rid)

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                items_with_artwork, discogs_service, song="Song"
            )

        assert len(results) == 3
        for i, (item, enriched) in enumerate(results, start=1):
            assert item.id == i, f"Item order not preserved at position {i}"
            assert enriched is not None
            assert enriched.release_year == 2000 + i

    @pytest.mark.asyncio
    async def test_to_match_result_includes_enriched_fields(self):
        result = DiscogsSearchResult(
            release_id=123,
            release_url="https://discogs.com/release/123",
            release_year=1997,
            artist_bio="A great artist.",
            wikipedia_url="https://en.wikipedia.org/wiki/Artist",
            spotify_url="https://open.spotify.com/search/Artist%20Song",
        )

        match = result.to_match_result()
        assert match.release_year == 1997
        assert match.artist_bio == "A great artist."
        assert match.wikipedia_url == "https://en.wikipedia.org/wiki/Artist"
        assert match.spotify_url == "https://open.spotify.com/search/Artist%20Song"
