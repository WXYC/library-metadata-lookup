"""Tests for metadata enrichment (release year, artist details, streaming links)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import (
    ArtistDetails,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    TrackItem,
)
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
    async def test_top1_gating_only_release_details_for_first_item(self):
        """Only ``results[0]`` should trigger the release+artist Discogs fetch.

        Previously every result in ``items_with_artwork`` ran
        ``fetch_release_details`` even though BS/iOS only consume the top
        result — paying N round-trips of Discogs cache (and on miss, API)
        latency for nothing. The orchestrator now gates the expensive
        enrichment to ``items_with_artwork[0]`` while keeping the cheap
        streaming-URL fallback per-result.

        Item order must still be preserved, and non-top-1 items keep their
        artwork object (with streaming URLs filled in).
        """
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
            results = await enrich_artwork_results(items_with_artwork, discogs_service, song="Song")

        assert len(results) == 3
        for i, (item, enriched) in enumerate(results, start=1):
            assert item.id == i, f"Item order not preserved at position {i}"
            assert enriched is not None
            # Streaming URLs still built per-result (cheap; no I/O).
            assert enriched.spotify_url is not None

        # Top-1 gets the release year; the rest don't.
        assert results[0][1].release_year == 2001
        assert results[1][1].release_year is None
        assert results[2][1].release_year is None

        # And only one release fetch fired — for release_id 1.
        called_ids = {call.args[0] for call in discogs_service.get_release.call_args_list}
        assert called_ids == {1}


class TestEnrichArtworkResultsExtended:
    """Coverage for the ``extended=True`` path: populates the additional
    DiscogsMatchResult fields LML already loads but normally discards.

    These fields land on the top-1 result only; non-top-1 items see the
    same (lean) shape as before."""

    @pytest.mark.asyncio
    async def test_extended_populates_new_fields_on_top1(self):
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=10154369, artist="Stereolab", album="Aluminum Tunes"
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=10154369,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1997,
            label="Duophonic Ultra High Frequency Disks",
            artist_id=42,
            genres=["Electronic", "Rock"],
            styles=["Indie Rock", "Experimental"],
            tracklist=[
                TrackItem(position="1", title="Pop Quiz", duration="3:14", artists=[]),
                TrackItem(position="2", title="Olv 26", duration="2:48", artists=[]),
            ],
            released="1997-09-22",
            release_url="https://discogs.com/release/10154369",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British band founded in 1990.",
            image_url="https://img.discogs.com/stereolab.jpg",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                song="Olv 26",
                extended=True,
            )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.discogs_artist_id == 42
        assert enriched.label == "Duophonic Ultra High Frequency Disks"
        assert enriched.genres == ["Electronic", "Rock"]
        assert enriched.styles == ["Indie Rock", "Experimental"]
        assert enriched.full_release_date == "1997-09-22"
        assert enriched.artist_image_url == "https://img.discogs.com/stereolab.jpg"
        assert enriched.tracklist is not None
        assert len(enriched.tracklist) == 2
        assert enriched.tracklist[0].title == "Pop Quiz"
        # The pre-existing year/bio/wiki fields also remain populated.
        assert enriched.release_year == 1997
        assert "French-British band" in enriched.artist_bio

    @pytest.mark.asyncio
    async def test_extended_false_leaves_new_fields_unset(self):
        """With extended=False the response shape is unchanged.

        Existing consumers (request-o-matic, dj-site proxy, BS request-line)
        must not see the new fields populated when they don't ask for them.
        """
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=123,
            title="x",
            artist="x",
            year=2020,
            artist_id=42,
            genres=["Rock"],
            styles=["Indie Rock"],
            label="Some Label",
            released="2020-01-15",
            release_url="https://discogs.com/release/123",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="x",
            image_url="https://img.discogs.com/x.jpg",
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                [(item, artwork)], discogs_service, extended=False
            )

        _, enriched = results[0]
        assert enriched.discogs_artist_id is None
        assert enriched.label is None
        assert enriched.genres is None
        assert enriched.styles is None
        assert enriched.full_release_date is None
        assert enriched.artist_image_url is None
        assert enriched.tracklist is None
        assert enriched.profile_tokens is None

    @pytest.mark.asyncio
    async def test_extended_parses_profile_tokens_cache_only(self):
        """When extended=True, the bio is parsed against the local cache only.

        References that resolve from the cache (typically [a<id>] for an
        artist whose details are already loaded by ``enrich_one``) become
        typed tokens; refs that miss fall through as plain text. No new
        Discogs API calls fire — only the cache_service lookups.
        """
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=123,
            title="x",
            artist="x",
            year=2020,
            artist_id=42,
            release_url="https://discogs.com/release/123",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="Self-reference: [a42]. Unknown release: [r999999].",
        )

        # cache_service that resolves artist 42 but knows no releases.
        cache_service = AsyncMock()
        cache_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="Stereolab"
        )
        cache_service.get_release.return_value = None  # unknown

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                extended=True,
                discogs_cache=cache_service,
            )

        _, enriched = results[0]
        assert enriched.profile_tokens is not None
        # The artist link resolved against the cache; the release ref didn't.
        kinds = [type(t).__name__ for t in enriched.profile_tokens]
        assert "ArtistLinkToken" in kinds
        # Cache-only resolver returns None for the unknown release; the [r…]
        # token is dropped by the resolver, not promoted to ReleaseLinkToken.
        assert "ReleaseLinkToken" not in kinds

    @pytest.mark.asyncio
    async def test_warm_cache_schedules_background_task(self):
        """warm_cache=True schedules a fire-and-forget deep-async bio parse.

        The orchestrator must NOT await the warming task — it runs after
        the response is built so write-path callers (Backend-Service's
        flowsheet-linkage) pay zero added latency.
        """
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=123,
            title="x",
            artist="x",
            year=2020,
            artist_id=42,
            release_url="https://discogs.com/release/123",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="x", profile="Bio with [a99] and [r77]."
        )

        scheduled: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def spy_create_task(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            scheduled.append(task)
            return task

        with (
            patch("lookup.orchestrator._fetch_apple_music_url", return_value=None),
            patch("lookup.orchestrator.asyncio.create_task", side_effect=spy_create_task),
        ):
            await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                extended=True,
                warm_cache=True,
            )

        # Exactly one warming task scheduled (top-1 only).
        assert len(scheduled) == 1
        # Drain it so the test exits cleanly. The task runs parse_async
        # against the API-capable resolver; with a mocked discogs_service
        # the resolutions return values from the side_effect.
        await asyncio.gather(*scheduled, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_warm_cache_false_does_not_schedule(self):
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=123,
            title="x",
            artist="x",
            year=2020,
            artist_id=42,
            release_url="https://discogs.com/release/123",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="x", profile="Bio."
        )

        scheduled: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def spy_create_task(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            scheduled.append(task)
            return task

        with (
            patch("lookup.orchestrator._fetch_apple_music_url", return_value=None),
            patch("lookup.orchestrator.asyncio.create_task", side_effect=spy_create_task),
        ):
            await enrich_artwork_results(
                [(item, artwork)], discogs_service, extended=True, warm_cache=False
            )

        assert len(scheduled) == 0

    @pytest.mark.asyncio
    async def test_extended_with_empty_results(self):
        """extended=True with no results is a no-op — no IndexError."""
        results = await enrich_artwork_results([], AsyncMock(), extended=True)
        assert results == []

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
