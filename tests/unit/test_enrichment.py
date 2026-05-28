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
    @staticmethod
    def _mock_client(results: list[dict]):
        import httpx

        mock_response = httpx.Response(
            200,
            json={"results": results},
            request=httpx.Request("GET", "https://itunes.apple.com/search"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    @pytest.mark.asyncio
    async def test_returns_url_on_success(self):
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Kate Bush",
                    "trackName": "The Saxophone Song",
                    "trackViewUrl": "https://music.apple.com/us/album/test/123",
                }
            ]
        )

        result = await _fetch_apple_music_url(
            "Kate Bush", "The Saxophone Song", http_client=mock_client
        )
        assert result == "https://music.apple.com/us/album/test/123"

    @pytest.mark.asyncio
    async def test_skips_wrong_artist_and_picks_correct_lower_result(self):
        """Regression for the Pleasure/'Joyous' -> Sheryl Crow mismatch (#389).

        iTunes Search relevance is unstable for obscure artists and can rank a
        popular but wrong artist first. The correct match must be chosen over a
        higher-ranked wrong-artist result, not blindly taken from results[0].
        """
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Sheryl Crow",
                    "trackName": "All I Wanna Do",
                    "trackViewUrl": "https://music.apple.com/us/album/all-i-wanna-do/1440651031",
                },
                {
                    "artistName": "Pleasure",
                    "trackName": "Joyous",
                    "trackViewUrl": "https://music.apple.com/us/album/joyous/1568229263",
                },
            ]
        )

        result = await _fetch_apple_music_url("Pleasure", "Joyous", http_client=mock_client)
        assert result == "https://music.apple.com/us/album/joyous/1568229263"

    @pytest.mark.asyncio
    async def test_returns_none_when_only_wrong_artist(self):
        """A confident-but-wrong link is worse than no link: reject, don't return it."""
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Sheryl Crow",
                    "trackName": "All I Wanna Do",
                    "trackViewUrl": "https://music.apple.com/us/album/all-i-wanna-do/1440651031",
                }
            ]
        )

        result = await _fetch_apple_music_url("Pleasure", "Joyous", http_client=mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_matches_despite_diacritics(self):
        """Diacritic-folded comparison: 'Nilüfer Yanya' must match iTunes' 'Nilufer Yanya'."""
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Nilufer Yanya",
                    "trackName": "Stabilise",
                    "trackViewUrl": "https://music.apple.com/us/album/stabilise/1480000000",
                }
            ]
        )

        result = await _fetch_apple_music_url("Nilüfer Yanya", "Stabilise", http_client=mock_client)
        assert result == "https://music.apple.com/us/album/stabilise/1480000000"

    @pytest.mark.asyncio
    async def test_skips_result_missing_track_url(self):
        """A matching result with no trackViewUrl is skipped in favor of a usable one."""
        mock_client = self._mock_client(
            [
                {"artistName": "Pleasure", "trackName": "Joyous"},
                {
                    "artistName": "Pleasure",
                    "trackName": "Joyous",
                    "trackViewUrl": "https://music.apple.com/us/album/joyous/1568229263",
                },
            ]
        )

        result = await _fetch_apple_music_url("Pleasure", "Joyous", http_client=mock_client)
        assert result == "https://music.apple.com/us/album/joyous/1568229263"

    @pytest.mark.asyncio
    async def test_rejects_right_track_on_wrong_album(self):
        """Regression for the Yenbett -> Tzenni mismatch (#396).

        Same-named track on multiple of the artist's releases: iTunes returns
        the older more-indexed album's track; without album verification,
        artist 100 + track 100 slip past #390's floor and persist the
        wrong-album URL.
        """
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Noura Mint Seymali",
                    "trackName": "Hebebeb (Zrag)",
                    "collectionName": "Tzenni",
                    "trackViewUrl": "https://music.apple.com/us/album/hebebeb-zrag/882843574?i=882843707",
                }
            ]
        )

        result = await _fetch_apple_music_url(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett", http_client=mock_client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_when_album_matches(self):
        """Album verification passes when the requested album matches the result's collection."""
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Noura Mint Seymali",
                    "trackName": "Hebebeb (Zrag)",
                    "collectionName": "Tzenni",
                    "trackViewUrl": "https://music.apple.com/us/album/tzenni/882843574?i=882843692",
                }
            ]
        )

        result = await _fetch_apple_music_url(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Tzenni", http_client=mock_client
        )
        assert result == "https://music.apple.com/us/album/tzenni/882843574?i=882843692"

    @pytest.mark.asyncio
    async def test_accepts_album_reissue_variant(self):
        """token_set_ratio handles reissue/edition variants ('Tzenni' vs 'Tzenni (Deluxe Edition)')."""
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Noura Mint Seymali",
                    "trackName": "Hebebeb (Zrag)",
                    "collectionName": "Tzenni (Deluxe Edition)",
                    "trackViewUrl": "https://music.apple.com/us/album/tzenni-deluxe/9999",
                }
            ]
        )

        result = await _fetch_apple_music_url(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Tzenni", http_client=mock_client
        )
        assert result == "https://music.apple.com/us/album/tzenni-deluxe/9999"

    @pytest.mark.asyncio
    async def test_omitted_album_skips_album_check(self):
        """Backward compat: when no album context is passed, verification mirrors pre-#396 behavior."""
        mock_client = self._mock_client(
            [
                {
                    "artistName": "Noura Mint Seymali",
                    "trackName": "Hebebeb (Zrag)",
                    "collectionName": "Tzenni",
                    "trackViewUrl": "https://music.apple.com/us/album/tzenni/882843574?i=882843692",
                }
            ]
        )

        result = await _fetch_apple_music_url(
            "Noura Mint Seymali", "Hebebeb (Zrag)", http_client=mock_client
        )
        assert result == "https://music.apple.com/us/album/tzenni/882843574?i=882843692"

    @pytest.mark.asyncio
    async def test_returns_none_on_no_results(self):
        mock_client = self._mock_client([])

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
        """An item with no Discogs match still gets a synthesized streaming-URL
        artwork block (``release_id=0`` sentinel marks the synthetic shape).

        Per LML#401 / BS#1184: the no-Discogs-match path used to short-circuit
        with ``artwork=None``, leaving releases that ARE on streaming but
        ISN'T in the WXYC catalog (e.g. Tragic Magic) with no iOS streaming
        buttons at all. The synthetic result surfaces the existing
        search-URL fallbacks (Spotify/YT/BC/SC) without inviting BS's
        ``extractAlbumMetadata`` projection — the ``release_id=0`` sentinel
        is what Backend-Service (BS#1185) keys off of to skip album-derived
        projections while still consuming streaming URLs.
        """
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        # No ``_fetch_apple_music_url`` patch — exercises the real
        # ``http_client=None`` graceful-degrade path (function returns None).
        results = await enrich_artwork_results([(item, None)], AsyncMock(), song="French Disko")

        _, enriched = results[0]
        assert enriched is not None
        # Sentinel: BS uses these to identify a streaming-only synthetic
        # result and skip extractAlbumMetadata.
        assert enriched.release_id == 0
        assert enriched.release_url == ""
        # Positional gating preserved: no album-derived fields anywhere.
        assert enriched.release_year is None
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None
        # apple_music_url stays None because http_client is None.
        assert enriched.apple_music_url is None
        # Search-URL fallbacks fill (artist + song are non-empty).
        assert enriched.spotify_url is not None
        assert enriched.youtube_music_url is not None
        assert enriched.bandcamp_url is not None
        assert enriched.soundcloud_url is not None

    @pytest.mark.asyncio
    async def test_no_discogs_match_surfaces_apple_url_via_itunes(self):
        """When ``_fetch_apple_music_url`` clears the 80/80/80 floor on a
        no-Discogs-match item, the synthesized artwork carries the URL.

        This is the Tragic Magic case from BS#1184 in miniature: an album
        absent from the WXYC catalog but present on Apple Music.
        """
        item = make_library_item(
            artist="Julianna Barwick & Mary Lattimore",
            title="The Four Sleeping Princesses",
        )

        apple_url = "https://music.apple.com/us/album/tragic-magic/1843854211"
        with patch(
            "lookup.orchestrator._fetch_apple_music_url",
            return_value=apple_url,
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="The Four Sleeping Princesses",
                album="Tragic Magic",
            )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_id == 0
        assert enriched.apple_music_url == apple_url
        # Album-derived fields stay None even though Apple matched — those
        # are positionally gated and require a real Discogs artwork.
        assert enriched.release_year is None
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_no_discogs_match_preserves_positional_gating_for_lower_items(self):
        """When ``items_with_artwork[0]`` has no artwork, no item in the
        response carries album-derived fields — even items further down
        that DO have artwork. The positional invariant from
        ``enrich_artwork_results``' docstring stays intact post-#401.
        """
        items_with_artwork: list[tuple[object, DiscogsSearchResult | None]] = [
            (
                make_library_item(id=1, artist="Artist 1", title="Album 1"),
                None,  # top-1 has no artwork
            ),
            (
                make_library_item(id=2, artist="Artist 2", title="Album 2"),
                make_discogs_result(release_id=42, artist="Artist 2", album="Album 2"),
            ),
        ]

        discogs_service = AsyncMock()
        # Even if Discogs would return data for the lower item, top-1 gating
        # means the orchestrator never calls it.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=42,
            title="Album 2",
            artist="Artist 2",
            year=2020,
            artist_id=99,
            release_url="https://discogs.com/release/42",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Artist 2",
            profile="bio",
            urls=["https://en.wikipedia.org/wiki/Artist_2"],
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(items_with_artwork, discogs_service, song="Song")

        # Both items return non-None artwork (synthetic for top-1, real for
        # lower); but neither carries album-derived fields.
        for _, enriched in results:
            assert enriched is not None
            assert enriched.release_year is None
            assert enriched.artist_bio is None
            assert enriched.wikipedia_url is None

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
    async def test_warm_cache_with_empty_bio_does_not_schedule(self):
        """warm_cache=True must not schedule the task when the top-1 bio is
        empty/None. Otherwise the warm task fires `parse_async("", …)` which
        wastes a coroutine creation and amplifies nothing.
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
        # Artist details with no profile bio — top1_bio resolves to None.
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="x", profile=None
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
                [(item, artwork)], discogs_service, extended=True, warm_cache=True
            )

        assert len(scheduled) == 0

    @pytest.mark.asyncio
    async def test_warm_task_anchored_so_gc_cannot_reap_it(self):
        """asyncio.create_task returns a weak reference. The orchestrator
        must park the task in a strong-ref container until it completes,
        or the GC can drop the warm mid-execution. Verify the task lands
        in the module-level set and is removed via the done_callback.
        """
        from lookup.orchestrator import _background_tasks

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

        snapshot_before = set(_background_tasks)

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                extended=True,
                warm_cache=True,
            )

        # The task is anchored in _background_tasks at scheduling time.
        added = _background_tasks - snapshot_before
        assert len(added) == 1
        # Drain so the done_callback runs and the task is removed.
        await asyncio.gather(*added, return_exceptions=True)
        # After completion, the set must shrink back — leaving no leak.
        assert (_background_tasks - snapshot_before) == set()

    @pytest.mark.asyncio
    async def test_extended_falls_back_to_sync_parse_without_cache(self):
        """When discogs_cache is None (LML deployed without
        DATABASE_URL_DISCOGS), bio parsing falls back to sync parse() so
        profile_tokens is non-None and consistent for client rendering.
        Sync parse() drops ID-based refs but keeps name and formatting
        tokens.
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
        # Bio with both name-based ([a=Name], renders) and id-based ([a42],
        # dropped) refs plus a bold span (kept).
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="Members [a=Tim Gane] and [a42]. [b]Active since 1990.[/b]",
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                extended=True,
                discogs_cache=None,  # Explicit: no cache available
            )

        _, enriched = results[0]
        assert enriched.profile_tokens is not None
        kinds = [type(t).__name__ for t in enriched.profile_tokens]
        # Name-based artist ref kept; id-based ref dropped (sync parse).
        assert kinds.count("ArtistLinkToken") == 1
        # Bold formatting kept.
        assert "BoldToken" in kinds

    @pytest.mark.asyncio
    async def test_top1_with_no_artwork_leaves_all_release_year_none(self):
        """Top-1 gating is *positional*. If items_with_artwork[0][1] is
        None, no item gets release-year enrichment — even items further
        down that have artwork. BS/iOS only consume results[0] so this is
        fine in practice; documented in the function's docstring.

        Post-LML#401: position 0 is now a synthesized streaming-only
        ``DiscogsSearchResult`` (release_id=0 sentinel) rather than None,
        so a release that's on Apple Music can still surface a streaming
        button on the no-Discogs-match path. The positional-gating
        invariant still holds — both positions have ``release_year=None``.
        """
        items_with_artwork = [
            (make_library_item(id=1), None),
            (make_library_item(id=2), make_discogs_result(release_id=2)),
        ]

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=2,
            title="x",
            artist="x",
            year=2020,
            artist_id=None,
            release_url="https://discogs.com/release/2",
        )

        with patch("lookup.orchestrator._fetch_apple_music_url", return_value=None):
            results = await enrich_artwork_results(items_with_artwork, discogs_service)

        # Position 0 is now a synthetic streaming-only result (release_id=0
        # sentinel marks it for BS#1185 to skip extractAlbumMetadata);
        # position 1 keeps its real artwork. Neither carries release_year.
        assert results[0][1] is not None
        assert results[0][1].release_id == 0
        assert results[0][1].release_url == ""
        assert results[0][1].release_year is None
        assert results[1][1] is not None
        assert results[1][1].release_id == 2
        assert results[1][1].release_year is None
        # No release fetch fired — fetch_top1_release_details short-circuits
        # when top-1's artwork is None.
        discogs_service.get_release.assert_not_called()

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
