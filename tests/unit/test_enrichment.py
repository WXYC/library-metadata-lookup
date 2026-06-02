"""Tests for metadata enrichment (release year, artist details, streaming links)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import (
    ArtistDetails,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    TrackItem,
)
from lookup.orchestrator import (
    _build_streaming_search_url,
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

        results = await enrich_artwork_results([(item, artwork)], discogs_service)
        _, enriched = results[0]
        assert enriched.release_year == 2020
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_handles_none_artwork(self):
        """No-Discogs-match items return a synthesized streaming-only
        ``DiscogsSearchResult`` (``release_id=0`` sentinel; see LML#401).
        With ``apple_music=None`` the Apple lookup degrades gracefully and
        only the search-URL fallbacks fill.
        """
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        # No ``apple_music`` argument — exercises the graceful-degrade path
        # (the orchestrator skips the probe when the client is None).
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
        # apple_music_url stays None because apple_music is None.
        assert enriched.apple_music_url is None
        # Search-URL fallbacks fill (artist + song are non-empty).
        assert enriched.spotify_url is not None
        assert enriched.youtube_music_url is not None
        assert enriched.bandcamp_url is not None
        assert enriched.soundcloud_url is not None

    @pytest.mark.asyncio
    async def test_no_discogs_match_surfaces_apple_url_via_apple_music(self):
        """When ``AppleMusicClient.find_track_url`` clears the 80/80/80 floor
        on a no-Discogs-match item, the synthesized artwork carries the URL.

        This is the Tragic Magic case from BS#1184 in miniature: an album
        absent from the WXYC catalog but present on Apple Music.
        """
        item = make_library_item(
            artist="Julianna Barwick & Mary Lattimore",
            title="The Four Sleeping Princesses",
        )

        apple_url = "https://music.apple.com/us/album/tragic-magic/1843854211"
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value=apple_url)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="The Four Sleeping Princesses",
            album="Tragic Magic",
            apple_music=apple_music,
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

        results = await enrich_artwork_results([(item, artwork)], discogs_service, extended=False)
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

        with patch("lookup.orchestrator.asyncio.create_task", side_effect=spy_create_task):
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

        with patch("lookup.orchestrator.asyncio.create_task", side_effect=spy_create_task):
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

        with patch("lookup.orchestrator.asyncio.create_task", side_effect=spy_create_task):
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


class TestMbTracklistRescue:
    """``mb_pg`` rescue: when the top-1 result has no Discogs artwork AND
    ``extended=true``, ``enrich_artwork_results`` calls
    ``resolve_tracklist_via_musicbrainz`` and stitches the tracklist onto
    the synth ``DiscogsSearchResult(release_id=0, ...)``. The picker's
    rotation-tracks controller then consumes it inline.

    Tests mock the resolver — no PG traffic.
    """

    @staticmethod
    def _track_items():
        from generated.api_models import DiscogsTrackItem

        return [
            DiscogsTrackItem(position="1", title="Brakhage", duration="4:12", artists=[]),
            DiscogsTrackItem(position="2", title="Cybele's Reverie", duration="4:05", artists=[]),
        ]

    @pytest.mark.asyncio
    async def test_synth_carries_mb_tracklist_when_resolver_hits(self):
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        _, enriched = results[0]
        assert enriched.release_id == 0  # synth sentinel preserved
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]
        resolver.assert_awaited_once_with("Stereolab", "Emperor Tomato Ketchup", mb_pg=mb_pg)

    @pytest.mark.asyncio
    async def test_no_resolver_call_when_extended_is_false(self):
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(),
        ) as resolver:
            await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                album="Emperor Tomato Ketchup",
                extended=False,
                mb_pg=mb_pg,
            )

        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_resolver_call_when_mb_pg_is_none(self):
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(),
        ) as resolver:
            await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=None,
            )

        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_resolver_call_when_top1_has_artwork(self):
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        artwork = make_discogs_result(release_id=42)
        mb_pg = AsyncMock()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=42,
            title="Emperor Tomato Ketchup",
            artist="Stereolab",
            year=1996,
            artist_id=None,
            release_url="https://discogs.com/release/42",
        )

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(),
        ) as resolver:
            await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_resolver_call_when_album_empty(self):
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(),
        ) as resolver:
            await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                album=None,
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_synth_tracklist_none_when_resolver_misses(self):
        # Resolver returned ``None`` (low similarity / no row / DB error).
        # The synth still composes; ``tracklist`` stays absent / falsy.
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")
        mb_pg = AsyncMock()

        with patch(
            "lookup.orchestrator.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=None),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                album="Aluminum Tunes",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        assert enriched.release_id == 0
        # Either absent or an empty/None-equivalent tracklist — both fine.
        assert not enriched.tracklist


class TestEnrichArtworkResultsWithAppleMusicClient:
    """LML#443/#444: orchestrator hot path consumes ``AppleMusicClient.find_track_url``.

    These tests pin the contract — ``enrich_artwork_results`` accepts an
    ``apple_music`` client and threads the call.
    """

    @pytest.mark.asyncio
    async def test_apple_music_client_url_propagates_to_synthesized_result(self):
        """A no-Discogs-match item paired with an ``AppleMusicClient`` that
        returns a URL produces a synthesized ``DiscogsSearchResult`` carrying
        that URL — mirrors LML#401 / BS#1184 (Tragic Magic on Apple Music but
        absent from the WXYC Discogs catalog).
        """
        item = make_library_item(
            artist="Julianna Barwick & Mary Lattimore",
            title="The Four Sleeping Princesses",
        )
        apple_url = "https://music.apple.com/us/album/tragic-magic/1843854211"

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value=apple_url)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="The Four Sleeping Princesses",
            album="Tragic Magic",
            apple_music=apple_music,
        )

        apple_music.find_track_url.assert_awaited_once_with(
            "Julianna Barwick & Mary Lattimore",
            "The Four Sleeping Princesses",
            album="Tragic Magic",
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_id == 0
        assert enriched.apple_music_url == apple_url

    @pytest.mark.asyncio
    async def test_apple_music_client_none_degrades_gracefully(self):
        """When ``apple_music`` is None (creds unconfigured), the lookup
        produces a result with ``apple_music_url`` None instead of raising.
        Matches the Spotify L1 degradation pattern in
        ``streaming/dependencies.py:get_spotify_client``.
        """
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="French Disko",
            album="Aluminum Tunes",
            apple_music=None,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url is None
        # Search-URL fallbacks for the OTHER services still fill.
        assert enriched.spotify_url is not None

    @pytest.mark.asyncio
    async def test_apple_music_client_returning_none_leaves_url_unset(self):
        """An ``AppleMusicClient`` that scores no result below the floor
        returns None — the synthesized result's ``apple_music_url`` stays
        None, mirroring the legacy iTunes path's behavior for unmatched
        queries.
        """
        item = make_library_item(artist="Obscure Artist", title="Obscure Album")

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value=None)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Obscure Song",
            album="Obscure Album",
            apple_music=apple_music,
        )

        apple_music.find_track_url.assert_awaited_once()
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url is None

    @pytest.mark.asyncio
    async def test_apple_music_client_raising_does_not_sink_whole_batch(self):
        """A single ``find_track_url`` exception must NOT propagate through
        ``asyncio.gather`` and fail the entire enrichment. ``find_track_url``
        has no top-level exception handler around its result-iteration loop,
        so the orchestrator wraps the call defensively — bulk lookups would
        otherwise regress to 500 if Apple returns a malformed result.
        """
        items_with_artwork = [
            (make_library_item(id=1, artist="Artist 1", title="Album 1"), None),
            (make_library_item(id=2, artist="Artist 2", title="Album 2"), None),
        ]

        apple_music = AsyncMock(spec=AppleMusicClient)
        # Item 1 raises; item 2 returns a normal URL. Without a defensive
        # wrap at the call site, the gather() in enrich_artwork_results
        # cancels item 2's enrichment mid-flight and raises.
        item2_url = "https://music.apple.com/us/album/album-2/222"
        apple_music.find_track_url = AsyncMock(
            side_effect=[
                AttributeError("Apple returned a non-dict item"),
                item2_url,
            ]
        )

        results = await enrich_artwork_results(
            items_with_artwork,
            AsyncMock(),
            song="Song",
            album="Album",
            apple_music=apple_music,
        )

        assert len(results) == 2
        _, enriched_1 = results[0]
        _, enriched_2 = results[1]
        # Item 1 degraded to no Apple URL but did not crash the request.
        assert enriched_1 is not None
        assert enriched_1.apple_music_url is None
        # Item 2's enrichment was not collateral-damaged by item 1's raise.
        # (Top-1 positional gating means album-derived fields are still
        # None, but the streaming URL itself survives.)
        assert enriched_2 is not None

    @pytest.mark.asyncio
    async def test_apple_music_find_track_url_timeout_degrades_to_none(self):
        """LML#449 + LML#450: a single ``find_track_url`` call that exceeds
        the call-site timeout must NOT pull the rest of the request past its
        deadline. The orchestrator wraps the call in ``asyncio.wait_for``
        with a tight ceiling; on TimeoutError the item degrades to no Apple
        URL, same as the LML#444 exception path. Pins the upper bound so the
        worst-case 4×60s retry loop in LML#450 cannot block enrichment.
        """
        items_with_artwork = [
            (make_library_item(id=1, artist="Mūm", title="Summer Make Good"), None),
            (make_library_item(id=2, artist="Stereolab", title="Aluminum Tunes"), None),
        ]

        apple_music = AsyncMock(spec=AppleMusicClient)

        # Sleep longer than any sane timeout. The wait_for ceiling triggers
        # before this completes; the test then verifies graceful degradation.
        async def slow_find(*_args, **_kwargs):
            await asyncio.sleep(60)
            return "https://music.apple.com/should-never-resolve"

        apple_music.find_track_url = AsyncMock(side_effect=slow_find)

        # Patch the call-site timeout to a tiny value so the test runs in
        # milliseconds and the asyncio.wait_for ceiling fires deterministically.
        with patch("lookup.orchestrator._apple_music_lookup_timeout_s", return_value=0.05):
            results = await enrich_artwork_results(
                items_with_artwork,
                AsyncMock(),
                song="Song",
                album="Album",
                apple_music=apple_music,
            )

        assert len(results) == 2
        _, enriched_1 = results[0]
        _, enriched_2 = results[1]
        # Both items degraded to no Apple URL; neither propagated TimeoutError
        # through the enclosing gather() to fail the whole batch.
        assert enriched_1 is not None
        assert enriched_1.apple_music_url is None
        assert enriched_2 is not None
        assert enriched_2.apple_music_url is None

    @pytest.mark.asyncio
    async def test_apple_music_find_track_url_timeout_projects_sentry_marker(self):
        """LML#462: on TimeoutError, the active Sentry transaction must carry
        a `data.apple_music.find_track_url.timeout = True` attribute so trace
        explorer can distinguish 'Apple Music timed out' from 'Apple Music
        never ran' — the LML#444 silent-failure shape, just for a different
        failure mode.

        Mirrors the `_log_resolver_pre_pass` / `_log_album_title_fallback`
        shape (lookup/orchestrator.py:262-298, 393-415): project onto
        `sentry_sdk.get_current_scope().transaction.set_data(...)` with a
        try/except so observability never breaks /lookup. The logger.warning
        line stays — this test only pins the additional Sentry projection.
        """
        items_with_artwork = [
            (make_library_item(id=1, artist="Mūm", title="Summer Make Good"), None),
        ]

        apple_music = AsyncMock(spec=AppleMusicClient)

        async def slow_find(*_args, **_kwargs):
            await asyncio.sleep(60)
            return "https://music.apple.com/should-never-resolve"

        apple_music.find_track_url = AsyncMock(side_effect=slow_find)

        transaction = MagicMock()
        scope = MagicMock()
        scope.transaction = transaction

        with (
            patch("lookup.orchestrator._apple_music_lookup_timeout_s", return_value=0.05),
            patch(
                "lookup.orchestrator.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            results = await enrich_artwork_results(
                items_with_artwork,
                AsyncMock(),
                song="Song",
                album="Album",
                apple_music=apple_music,
            )

        # User-visible behavior unchanged: item still degrades to no Apple URL.
        assert len(results) == 1
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url is None

        # AND the timeout was projected onto the active transaction so trace
        # explorer can filter for it.
        transaction.set_data.assert_any_call("apple_music.find_track_url.timeout", True)

    @pytest.mark.asyncio
    async def test_apple_music_find_track_url_timeout_survives_no_active_transaction(
        self,
    ):
        """LML#462: when there is no active Sentry transaction (or the SDK
        raises), the timeout marker projection must NOT break /lookup. Same
        swallow pattern as `_log_album_title_fallback` / `_log_resolver_pre_pass`.
        """
        items_with_artwork = [
            (make_library_item(id=1, artist="Mūm", title="Summer Make Good"), None),
        ]

        apple_music = AsyncMock(spec=AppleMusicClient)

        async def slow_find(*_args, **_kwargs):
            await asyncio.sleep(60)
            return "https://music.apple.com/should-never-resolve"

        apple_music.find_track_url = AsyncMock(side_effect=slow_find)

        # No active transaction.
        scope = MagicMock()
        scope.transaction = None

        with (
            patch("lookup.orchestrator._apple_music_lookup_timeout_s", return_value=0.05),
            patch(
                "lookup.orchestrator.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            results = await enrich_artwork_results(
                items_with_artwork,
                AsyncMock(),
                song="Song",
                album="Album",
                apple_music=apple_music,
            )

        # Degradation still works; no crash from the missing transaction.
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url is None

    @pytest.mark.asyncio
    async def test_apple_music_url_does_not_override_db_streaming_link(self):
        """Direct streaming-links from the library DB take precedence over the
        AppleMusicClient probe — preserves the existing
        ``apple_music_override or apple_music_result or None`` priority.
        """
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1, artist="Jessica Pratt", album="On Your Own Love Again"
        )

        db_url = "https://music.apple.com/us/album/db-override/111"
        probe_url = "https://music.apple.com/us/album/probe-result/222"

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value=probe_url)

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(return_value={"apple_music_url": db_url})

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="On Your Own Love Again",
            artist="Jessica Pratt",
            year=2015,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Back, Baby",
            album="On Your Own Love Again",
            library_db=library_db,
            apple_music=apple_music,
        )

        _, enriched = results[0]
        assert enriched.apple_music_url == db_url


class TestStreamingLinksTitleGate:
    """LML#477: gate library_db.streaming_links projection on a title-confidence
    floor against the requested album.

    Library search's fuzzy fallback (``_fuzzy_search`` in ``library/db.py``)
    accepts ``token_set_ratio >= 70`` — permissive enough to surface
    sibling-artist rows where the artist matches but the album doesn't
    (e.g. requesting "Yenbett" returns "Tzenni" because both are Noura
    Mint Seymali albums). Without a gate at the orchestrator's
    streaming-link projection site, those rows' verified Spotify/Apple
    URLs would propagate into the response as if they pointed at the
    requested album. The 80 floor mirrors ``is_acceptable_match`` in
    ``clients/streaming/matching.py``.
    """

    @pytest.mark.asyncio
    async def test_skips_streaming_links_when_row_title_mismatches_album(self):
        # Sibling-album fuzzy hit: artist matches, title doesn't — the
        # row's stored streaming URLs point at the sibling, not the
        # requested album.
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        artwork = make_discogs_result(release_id=1, artist="Noura Mint Seymali", album="Tzenni")

        wrong_spotify = "https://open.spotify.com/album/tzenni-id"
        wrong_apple = "https://music.apple.com/us/album/tzenni/111"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": wrong_spotify,
                "apple_music_url": wrong_apple,
                "youtube_music_url": "https://music.youtube.com/wrong",
                "bandcamp_url": "https://wrong.bandcamp.com/album/tzenni",
                "soundcloud_url": "https://soundcloud.com/wrong/tzenni",
            }
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            album="Yenbett",
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched is not None
        # No wrong-album URLs propagated.
        assert enriched.spotify_url != wrong_spotify
        assert enriched.apple_music_url != wrong_apple
        assert enriched.youtube_music_url != "https://music.youtube.com/wrong"
        assert enriched.bandcamp_url != "https://wrong.bandcamp.com/album/tzenni"
        assert enriched.soundcloud_url != "https://soundcloud.com/wrong/tzenni"
        # Synthesized search-URL fallback fills in instead.
        assert enriched.spotify_url is not None
        assert "search" in enriched.spotify_url.lower()
        # And we never even queried the DB for streaming links — the gate
        # short-circuits before the lookup.
        library_db.get_streaming_links.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_applies_streaming_links_when_row_title_matches_album(self):
        # Happy path: library row title matches the requested album.
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1, artist="Jessica Pratt", album="On Your Own Love Again"
        )

        verified_spotify = "https://open.spotify.com/album/oyola-id"
        verified_apple = "https://music.apple.com/us/album/oyola/222"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": verified_spotify,
                "apple_music_url": verified_apple,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="On Your Own Love Again",
            artist="Jessica Pratt",
            year=2015,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            album="On Your Own Love Again",
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched.spotify_url == verified_spotify
        assert enriched.apple_music_url == verified_apple

    @pytest.mark.asyncio
    async def test_applies_streaming_links_when_album_not_requested(self):
        # No requested album → no signal to gate against; preserve
        # legacy behavior of trusting the library row.
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")
        artwork = make_discogs_result(release_id=1, artist="Juana Molina", album="DOGA")

        verified_spotify = "https://open.spotify.com/album/doga-id"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": verified_spotify,
                "apple_music_url": None,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="DOGA",
            artist="Juana Molina",
            year=2022,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            album=None,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched.spotify_url == verified_spotify
