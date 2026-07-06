"""Tests for metadata enrichment (release year, artist details, streaming links)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    TrackItem,
)
from lookup.enrichment import enrich_artwork_results
from lookup.enrichment.item import _build_streaming_search_url
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
        # Spotify's templated search-URL fallback was deleted in LML#573 (the
        # persistent cache replaces it); with no Spotify client / flag here,
        # spotify_url stays None. YTM / Bandcamp / SoundCloud still fall back.
        assert enriched.spotify_url is None
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
        # (``enrich_artwork_results`` skips the probe when the client is None).
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
        # YTM / Bandcamp / SoundCloud search-URL fallbacks fill (artist + song
        # non-empty). Spotify's fallback was deleted in LML#573 → None here.
        assert enriched.spotify_url is None
        assert enriched.youtube_music_url is not None
        assert enriched.bandcamp_url is not None
        assert enriched.soundcloud_url is not None

    @pytest.mark.asyncio
    async def test_no_discogs_match_surfaces_apple_url_via_apple_music(self):
        """When ``AppleMusicClient.find_track_metadata`` clears the 80/80/80
        floor on a no-Discogs-match item, the synthesized artwork carries
        the URL.

        This is the Tragic Magic case from BS#1184 in miniature: an album
        absent from the WXYC catalog but present on Apple Music. Post-
        LML#487 the probe also lands ``artwork_url`` + ``release_year`` on
        the synthesized result; this test pins the URL-only invariant from
        the pre-LML#487 contract — ``artist_bio`` + ``wikipedia_url`` stay
        None because they require a real Discogs artwork (positional gate).
        """
        item = make_library_item(
            artist="Julianna Barwick & Mary Lattimore",
            title="The Four Sleeping Princesses",
        )

        apple_url = "https://music.apple.com/us/album/tragic-magic/1843854211"
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(url=apple_url, artwork_url=None, release_year=None)
        )

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
        # Discogs-derived album fields stay None — positionally gated and
        # require a real Discogs artwork.
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
        # means ``enrich_artwork_results`` never calls it.
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
        # Spotify templated fallback deleted (LML#573); the retained YTM/BC/SC
        # fallbacks still generate. spotify_url stays None with no client/flag.
        assert enriched.spotify_url is None
        assert enriched.youtube_music_url is not None  # retained fallback still works

    @pytest.mark.asyncio
    async def test_top1_gating_only_release_details_for_first_item(self):
        """Only ``results[0]`` should trigger the release+artist Discogs fetch.

        Previously every result in ``items_with_artwork`` ran the release
        fetch (now ``fetch_top1_release_details`` in ``lookup/enrichment/
        top1.py``) even though BS/iOS only consume the top
        result — paying N round-trips of Discogs cache (and on miss, API)
        latency for nothing. ``enrich_artwork_results`` now gates the expensive
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
            # Streaming URLs still built per-result (cheap; no I/O). Spotify's
            # templated fallback was deleted in LML#573 → None; YTM still fills.
            assert enriched.spotify_url is None
            assert enriched.youtube_music_url is not None

        # Top-1 gets the release year; the rest don't.
        assert results[0][1].release_year == 2001
        assert results[1][1].release_year is None
        assert results[2][1].release_year is None

        # And only one release fetch fired — for release_id 1.
        called_ids = {call.args[0] for call in discogs_service.get_release.call_args_list}
        assert called_ids == {1}


class TestTop1SentinelGuard:
    """`fetch_top1_release_details` must short-circuit when the top-1 artwork
    carries the LML#401 `release_id=0` sentinel — otherwise it round-trips
    `discogs_service.get_release(0)` which hits the Discogs API at
    `/releases/0` (404) before the `if not release` branch swallows the
    response. The sentinel is a documented cross-service contract; the
    enrichment owns the per-call-site gating (see issue #518)."""

    @pytest.mark.asyncio
    async def test_top1_artwork_with_release_id_zero_skips_get_release(self):
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")
        # The synthesized sentinel as it would arrive from LML#401's
        # streaming-only no-Discogs-match path.
        sentinel = DiscogsSearchResult(release_id=0, release_url="")

        discogs_service = AsyncMock()

        await enrich_artwork_results([(item, sentinel)], discogs_service, song="French Disko")

        discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_top1_artwork_with_real_release_id_calls_get_release_once(self):
        """Paired positive case: the guard must not fire on a real id.

        Also pins the invocation count at 1 so a regression that double-fetches
        the release (e.g. fallback-artwork resolution leaking into the top-1
        enrichment path) doesn't slip through."""
        item = make_library_item(artist="Cat Power", title="Moon Pix")
        artwork = make_discogs_result(release_id=12345, artist="Cat Power", album="Moon Pix")

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=12345,
            title="Moon Pix",
            artist="Cat Power",
            year=1998,
            artist_id=None,  # short-circuits before get_artist_details
            release_url="https://discogs.com/release/12345",
        )

        await enrich_artwork_results([(item, artwork)], discogs_service, song="Cross Bones Style")

        discogs_service.get_release.assert_called_once_with(12345)


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
            # LML#504: bio/wiki gate on the artist-identity split when
            # ``extended=True``. Pass the request artist so the gate
            # verifies request → item.artist → release.artist hops.
            artist="Stereolab",
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
    async def test_populates_master_id_on_top1_from_resolved_release(self):
        """LML#688: the resolved release's master_id lands on the enriched top-1
        result so a catalog-popularity caller can collapse pressings by master.

        master_id is a release-identity fact (like release_id), so it surfaces
        whenever the release resolves — independent of ``extended`` mode. This
        is the same enrichment path the bulk /lookup route runs per item, so a
        passing assertion here covers the bulk path's master_id carriage too.
        """
        item = make_library_item(artist="Juana Molina", title="DOGA")
        artwork = make_discogs_result(release_id=10154369, artist="Juana Molina", album="DOGA")

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=10154369,
            master_id=777001,
            title="DOGA",
            artist="Juana Molina",
            year=2022,
            artist_id=42,
            release_url="https://discogs.com/release/10154369",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Juana Molina",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="la paradoja",
            artist="Juana Molina",
            extended=True,
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.master_id == 777001

    @pytest.mark.asyncio
    async def test_master_less_release_surfaces_null_master_id(self):
        """A master-less release (one-off / self-released) surfaces master_id=None."""
        item = make_library_item(artist="Chuquimamani-Condori", title="Edits")
        artwork = make_discogs_result(
            release_id=20154369, artist="Chuquimamani-Condori", album="Edits"
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=20154369,
            master_id=None,
            title="Edits",
            artist="Chuquimamani-Condori",
            year=2023,
            artist_id=43,
            release_url="https://discogs.com/release/20154369",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=43,
            name="Chuquimamani-Condori",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Call Your Name",
            artist="Chuquimamani-Condori",
            extended=True,
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.master_id is None

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
    async def test_extended_populates_writer_credits_release_level(self):
        """LML#699: the release-level writer-role subset of the resolved
        release's extra_artists surfaces as writer_credits (provenance=release),
        derived from the already-fetched release with no new Discogs call.
        """
        item = make_library_item(artist="Juana Molina", title="DOGA")
        artwork = make_discogs_result(release_id=555, artist="Juana Molina", album="DOGA")

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=555,
            title="DOGA",
            artist="Juana Molina",
            artist_id=42,
            release_url="https://discogs.com/release/555",
            tracklist=[TrackItem(position="1", title="la paradoja", artists=[])],
            extra_artists=[
                ArtistCredit(name="Juana Molina", role="Written-By"),
                ArtistCredit(name="A Producer", role="Producer"),
            ],
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="Juana Molina"
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="la paradoja",
            artist="Juana Molina",
            extended=True,
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.writer_credits is not None
        assert enriched.writer_credits.names == ["Juana Molina"]
        assert enriched.writer_credits.provenance == "release"
        assert enriched.writer_credits.track_position is None
        # Cache-only: the writer credits came from the single already-fetched
        # release, not a second Discogs call.
        discogs_service.get_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_extended_writer_credits_track_level_when_song_resolves(self):
        """LML#699 Phase 2: when the played song resolves to a tracklist
        position carrying per-track writers, the response emits
        provenance="track" with that track's position — scoped to the playcut,
        not the release-level approximation, and no cross-track bleed.
        """
        item = make_library_item(artist="Sessa", title="Pequena Vertigem de Amor")
        artwork = make_discogs_result(
            release_id=558, artist="Sessa", album="Pequena Vertigem de Amor"
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=558,
            title="Pequena Vertigem de Amor",
            artist="Sessa",
            artist_id=42,
            release_url="https://discogs.com/release/558",
            tracklist=[
                TrackItem(position="A1", title="Sol Te Pegou", artists=[]),
                TrackItem(position="A2", title="Dor Fodida", artists=[]),
            ],
            track_writers={
                "A1": [ArtistCredit(name="A1 Writer", role="Written-By")],
                "A2": [ArtistCredit(name="A2 Writer", role="Composed By")],
            },
            extra_artists=[ArtistCredit(name="Sessa", role="Written-By")],
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(artist_id=42, name="Sessa")

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Sol Te Pegou",
            artist="Sessa",
            extended=True,
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.writer_credits is not None
        assert enriched.writer_credits.provenance == "track"
        assert enriched.writer_credits.track_position == "A1"
        assert enriched.writer_credits.names == ["A1 Writer"]
        assert "A2 Writer" not in enriched.writer_credits.names  # no cross-track bleed
        # Cache-only: derived from the single already-fetched release.
        discogs_service.get_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_extended_writer_credits_none_when_no_writer(self):
        """A resolved release whose extra_artists carry no writer role omits
        writer_credits (never fabricated).
        """
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(release_id=556, artist="Stereolab", album="Aluminum Tunes")

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=556,
            title="Aluminum Tunes",
            artist="Stereolab",
            artist_id=42,
            release_url="https://discogs.com/release/556",
            tracklist=[TrackItem(position="1", title="Olv 26", artists=[])],
            extra_artists=[ArtistCredit(name="An Engineer", role="Mixed By")],
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42, name="Stereolab"
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Olv 26",
            artist="Stereolab",
            extended=True,
        )
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.writer_credits is None

    @pytest.mark.asyncio
    async def test_extended_false_leaves_writer_credits_unset(self):
        """With extended=False, writer_credits stays unset — unchanged shape for
        request-line / dj-site-proxy consumers, even when the release carries a
        writer credit.
        """
        item = make_library_item()
        artwork = make_discogs_result()

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=557,
            title="x",
            artist="x",
            artist_id=42,
            release_url="https://discogs.com/release/557",
            extra_artists=[ArtistCredit(name="x", role="Written-By")],
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(artist_id=42, name="x")

        results = await enrich_artwork_results([(item, artwork)], discogs_service, extended=False)
        _, enriched = results[0]
        assert enriched.writer_credits is None

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
            title="Aluminum Tunes",
            artist="Stereolab",
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
            artist="Stereolab",
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

        The enrichment must NOT await the warming task — it runs after
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

        with patch("lookup.enrichment.asyncio.create_task", side_effect=spy_create_task):
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

        with patch("lookup.enrichment.asyncio.create_task", side_effect=spy_create_task):
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

        with patch("lookup.enrichment.asyncio.create_task", side_effect=spy_create_task):
            await enrich_artwork_results(
                [(item, artwork)], discogs_service, extended=True, warm_cache=True
            )

        assert len(scheduled) == 0

    @pytest.mark.asyncio
    async def test_warm_task_anchored_so_gc_cannot_reap_it(self):
        """asyncio.create_task returns a weak reference. The enrichment
        must park the task in a strong-ref container until it completes,
        or the GC can drop the warm mid-execution. Verify the task lands
        in the module-level set and is removed via the done_callback.
        """
        from lookup.enrichment.background import _background_tasks

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
            title="Aluminum Tunes",
            artist="Stereolab",
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
            artist="Stereolab",
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

    @pytest.mark.asyncio
    async def test_extended_adds_no_incremental_discogs_calls(self):
        """Bulk-safety pin (LML#685): flipping ``extended`` on adds ZERO live
        Discogs API calls.

        The bulk backfill (BS#1442) drains ~35k albums through this same
        enrichment path with ``extended=True``. Its only safety margin is that
        every extended field is plucked from the already-fetched ``top1_release``
        / ``top1_details`` — or a cache-only bio parse / local MusicBrainz PG
        query — never a new Discogs fetch. ``fetch_top1_release_details`` runs
        ``get_release`` + ``get_artist_details`` regardless of ``extended`` (they
        feed the base ``release_year`` / ``artist_bio`` / ``wikipedia_url``
        scalars), so the extended payload rides those already-paid fetches.

        This pins the invariant directly: with identical input, ``get_release``
        and ``get_artist_details`` fire the SAME number of times whether
        ``extended`` is False or True. ``warm_cache`` stays at its default
        (False) — that is the separate flag that WOULD fan out per-ref Discogs
        calls via ``_warm_bio_cache``, and the bulk drain must never set it.
        """

        def _make_service() -> AsyncMock:
            svc = AsyncMock()
            svc.get_release.return_value = ReleaseMetadataResponse(
                release_id=10154369,
                title="Aluminum Tunes",
                artist="Stereolab",
                year=1997,
                label="Duophonic Ultra High Frequency Disks",
                artist_id=42,
                genres=["Electronic", "Rock"],
                styles=["Indie Rock", "Experimental"],
                tracklist=[TrackItem(position="1", title="Olv 26", artists=[])],
                released="1997-09-22",
                release_url="https://discogs.com/release/10154369",
            )
            svc.get_artist_details.return_value = ArtistDetails(
                artist_id=42,
                name="Stereolab",
                profile="French-British band founded in 1990. See [a42].",
                image_url="https://img.discogs.com/stereolab.jpg",
                urls=["https://en.wikipedia.org/wiki/Stereolab"],
            )
            return svc

        def _call_kwargs(svc: AsyncMock) -> dict:
            item = make_library_item(artist="Stereolab", title="Aluminum Tunes")
            artwork = make_discogs_result(
                release_id=10154369, artist="Stereolab", album="Aluminum Tunes"
            )
            return {
                "items_with_artwork": [(item, artwork)],
                "discogs_service": svc,
                "song": "Olv 26",
                "artist": "Stereolab",
            }

        base_svc = _make_service()
        await enrich_artwork_results(**_call_kwargs(base_svc), extended=False)

        extended_svc = _make_service()
        result = await enrich_artwork_results(**_call_kwargs(extended_svc), extended=True)

        # The extended run actually populated the top-1 extended payload...
        _, enriched = result[0]
        assert enriched is not None
        assert enriched.discogs_artist_id == 42
        assert enriched.tracklist is not None

        # ...yet made no MORE Discogs API calls than the non-extended run — and
        # exactly the one release + one artist fetch the base path already pays.
        assert extended_svc.get_release.call_count == base_svc.get_release.call_count == 1
        assert (
            extended_svc.get_artist_details.call_count
            == base_svc.get_artist_details.call_count
            == 1
        )
        # Method-agnostic backstop: total interactions with the Discogs service
        # are identical on-vs-off, so a FUTURE extended branch that reached a
        # different Discogs method (not just the two named above) would still
        # trip this pin.
        assert len(extended_svc.mock_calls) == len(base_svc.mock_calls)


class TestMbTracklistRescue:
    """``mb_pg`` rescue: when the top-1 result has no Discogs artwork AND
    ``extended=true``, ``enrich_artwork_results`` calls
    ``resolve_tracklist_via_musicbrainz`` and stitches the tracklist onto
    the synth ``DiscogsSearchResult(release_id=0, ...)``. The picker's
    rotation-tracks controller then consumes it inline.

    Tests mock the resolver — no PG traffic.
    """

    @pytest.fixture(autouse=True)
    def _isolate_rollback_flag(self, monkeypatch):
        """LML#506: the song-sanity check defaults to ON via
        ``_mb_rescue_song_match_required`` reading
        ``LML_MB_RESCUE_REQUIRE_SONG_MATCH`` at request time. A developer
        or CI environment with the flag set to ``false`` would silently
        invert every default-on assertion in this class. ``delenv`` the
        flag for every test in this group so the default-on behavior is
        what we actually exercise; tests that need flag=false set it
        explicitly via ``monkeypatch.setenv``.
        """
        monkeypatch.delenv("LML_MB_RESCUE_REQUIRE_SONG_MATCH", raising=False)

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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
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

    @pytest.mark.asyncio
    async def test_synth_rejects_mb_tracklist_when_song_absent(self):
        """LML#506 bonus-only-track Deluxe leak: DJ requests a bonus track
        unique to the Deluxe Edition; MB's ``LIMIT 1`` returns the Original
        Edition tracklist (long shared substring clears the 0.70 trigram
        cutoff). Sanity check rejects the surfaced tracklist because the
        requested song doesn't appear in it.
        """
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Bonus Track Only On Deluxe",
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        assert enriched.release_id == 0
        # Tracklist rejected — the requested song doesn't appear in MB's
        # candidate. Picker UI falls back to its empty-dropdown UX (same
        # behaviour as a hard resolver miss).
        assert not enriched.tracklist

    @pytest.mark.asyncio
    async def test_synth_carries_mb_tracklist_when_song_present(self):
        """Sanity check passes — requested song appears in the rescued
        tracklist. Tracklist is surfaced (happy path)."""
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Brakhage",
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        assert enriched.release_id == 0
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]

    @pytest.mark.asyncio
    async def test_synth_carries_mb_tracklist_when_song_is_none(self):
        """No song in the request (artist+album picker lookup) → skip the
        sanity check, surface the tracklist. The whole point of the check is
        track-presence verification; without a song there's nothing to verify
        against, and over-rejecting picker calls would silently break the
        artist+album entry path."""
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song=None,
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]

    @pytest.mark.asyncio
    async def test_sanity_check_handles_track_side_suffix_variants(self):
        """The sanity check uses ``score_match_track``, so requested-song
        names match MB-side track titles even when MB carries trailing
        ``(Live)`` / ``(Remix)`` / ``- Acoustic`` markers. Without
        track-side suffix stripping, the 80 floor over-rejects these
        (empirically: ``score_match("Brakhage", "Brakhage (Live)") == 69.6``).
        """
        from generated.api_models import DiscogsTrackItem

        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()
        tracklist_with_live_marker = [
            DiscogsTrackItem(position="1", title="Brakhage (Live)", duration="4:12", artists=[]),
            DiscogsTrackItem(
                position="2", title="Cybele's Reverie - Acoustic", duration="4:05", artists=[]
            ),
        ]

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=tracklist_with_live_marker),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Brakhage",
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        assert enriched.tracklist is not None  # passed the floor via suffix strip

    @pytest.mark.asyncio
    async def test_sanity_check_disabled_via_env_flag(self, monkeypatch):
        """``LML_MB_RESCUE_REQUIRE_SONG_MATCH=false`` reverts to pre-LML#506
        behaviour — tracklist is surfaced regardless of song presence. Lets an
        operator turn the check off via Railway env var without a redeploy
        if it over-rejects in production."""
        monkeypatch.setenv("LML_MB_RESCUE_REQUIRE_SONG_MATCH", "false")
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ) as resolver:
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Track Definitely Not In Tracklist",
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        resolver.assert_awaited_once()
        _, enriched = results[0]
        # Flag off → carry tracklist regardless of song-presence verdict.
        assert enriched.tracklist is not None

    @pytest.mark.asyncio
    async def test_shared_track_deluxe_leak_is_known_limitation(self):
        """Pin the LML#506 known limitation as a positive assertion: when
        the DJ requests a track present on BOTH Original and Deluxe
        editions and MB's ``LIMIT 1`` returns Original, the sanity check
        accepts the (wrong-edition) tracklist because the requested song
        DOES appear in it. This test asserts the leak goes through —
        when Option 2 (resolver-side top-K filtered by song-presence)
        ships and starts returning the right release, the maintainer
        must update this test to assert rejection instead.

        Note: this test cannot be a strict-xfail because the resolver
        is mocked with a constant; resolver-internal changes can't flip
        it. The deeper test (real resolver against a fake mb_pg with
        both editions present) is filed alongside Option 2.
        """
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup (Deluxe)")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),  # Original's tracklist
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Brakhage",  # exists on both Original and Deluxe
                album="Emperor Tomato Ketchup (Deluxe)",
                extended=True,
                mb_pg=mb_pg,
            )

        _, enriched = results[0]
        # Leak: tracklist surfaces despite the request asking for Deluxe
        # and MB returning Original. This is the known-limitation cohort.
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "song",
        [
            pytest.param("   ", id="whitespace-only"),
            pytest.param("\t", id="tab-only"),
            pytest.param(" \n ", id="mixed-whitespace"),
        ],
    )
    async def test_whitespace_song_skips_sanity_check(self, song):
        """LML#506 review: a whitespace-only ``song`` is truthy in Python.
        Without an upfront strip, it enters the sanity check, normalizes to
        empty inside ``score_match_track``, scores 0 against every track,
        and drops a legitimate MB rescue. The fix strips ``song`` upfront
        so whitespace-only inputs follow the same skip path as ``None``.
        """
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song=song,
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        _, enriched = results[0]
        # Sanity check skipped because the stripped song was blank; the
        # tracklist surfaces just as it would for ``song=None``.
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "song",
        [
            pytest.param("(Live)", id="paren-only-marker"),
            pytest.param("(feat. Some Artist)", id="paren-only-feat"),
            pytest.param("(Acoustic Version)", id="paren-compound-marker"),
        ],
    )
    async def test_all_marker_song_skips_sanity_check(self, song):
        """LML#506 review iter 2: when ``song`` is entirely variant-marker
        (a malformed parse), ``strip_track_suffix`` reduces it to "" and
        ``score_match("", t.title)`` returns 0 for every track. Without an
        upfront guard the sanity check would unconditionally drop the
        rescue and emit a misleading ``song_sanity_rejected=true`` signal.
        Treat the all-marker case the same as ``song=None`` / whitespace.
        """
        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=self._track_items()),
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song=song,
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        _, enriched = results[0]
        assert enriched.tracklist is not None
        assert [t.title for t in enriched.tracklist] == ["Brakhage", "Cybele's Reverie"]

    @pytest.mark.asyncio
    async def test_empty_track_titles_do_not_false_pass(self):
        """LML#506 review: ``score_match("", "")`` returns 100 by rapidfuzz
        convention. If MB surfaces a corrupted release whose tracklist is
        all-empty-title rows, the sanity check must NOT accept it just
        because the score-against-empty cleared the floor. The fix requires
        a non-empty stripped track title for the iteration to count as a
        hit.
        """
        from generated.api_models import DiscogsTrackItem

        item = make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        mb_pg = AsyncMock()
        # Corrupt-row shape: every title empty / whitespace-only. (The
        # resolver coerces NULL titles to "" via ``row.get("title") or ""``
        # before constructing the DiscogsTrackItem, so the enrichment code
        # only ever sees empty-string titles in this cohort, not None.)
        all_empty_titles = [
            DiscogsTrackItem(position="1", title="", duration=None, artists=[]),
            DiscogsTrackItem(position="2", title="   ", duration=None, artists=[]),
        ]

        with patch(
            "lookup.enrichment.item.resolve_tracklist_via_musicbrainz",
            new=AsyncMock(return_value=all_empty_titles),
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Brakhage",
                album="Emperor Tomato Ketchup",
                extended=True,
                mb_pg=mb_pg,
            )

        _, enriched = results[0]
        # No real track title to match against — sanity check rejects.
        assert not enriched.tracklist


class TestEnrichArtworkResultsWithAppleMusicClient:
    """LML#443/#444: enrichment hot path consumes ``AppleMusicClient.find_track_url``.

    These tests pin the contract — ``enrich_artwork_results`` accepts an
    ``apple_music`` client and threads the call.
    """

    @pytest.mark.asyncio
    async def test_apple_music_client_url_propagates_to_synthesized_result(self):
        """A no-Discogs-match item paired with an ``AppleMusicClient`` that
        returns a metadata match produces a synthesized ``DiscogsSearchResult``
        carrying that URL — mirrors LML#401 / BS#1184 (Tragic Magic on Apple
        Music but absent from the WXYC Discogs catalog). Post-LML#487 the
        synthesis path consumes ``find_track_metadata`` rather than the
        URL-only ``find_track_url`` so artwork + release year can land on
        the same response.
        """
        item = make_library_item(
            artist="Julianna Barwick & Mary Lattimore",
            title="The Four Sleeping Princesses",
        )
        apple_url = "https://music.apple.com/us/album/tragic-magic/1843854211"

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(url=apple_url, artwork_url=None, release_year=None)
        )

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="The Four Sleeping Princesses",
            album="Tragic Magic",
            apple_music=apple_music,
        )

        apple_music.find_track_metadata.assert_awaited_once_with(
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
        # Search-URL fallbacks for YTM/BC/SC still fill; Spotify's fallback was
        # deleted in LML#573 → None (no Spotify client/flag here).
        assert enriched.spotify_url is None
        assert enriched.youtube_music_url is not None

    @pytest.mark.asyncio
    async def test_apple_music_client_returning_none_leaves_url_unset(self):
        """An ``AppleMusicClient`` that scores no result below the floor
        returns None — the synthesized result's ``apple_music_url`` stays
        None, mirroring the legacy iTunes path's behavior for unmatched
        queries.
        """
        item = make_library_item(artist="Obscure Artist", title="Obscure Album")

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Obscure Song",
            album="Obscure Album",
            apple_music=apple_music,
        )

        apple_music.find_track_metadata.assert_awaited_once()
        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url is None

    @pytest.mark.asyncio
    async def test_apple_music_client_raising_does_not_sink_whole_batch(self):
        """A single ``find_track_metadata`` exception must NOT propagate
        through ``asyncio.gather`` and fail the entire enrichment — the
        underlying search-song loop has no top-level exception handler
        around its result iteration, so the enrichment wraps the call
        defensively. Bulk lookups would otherwise regress to 500 if Apple
        returns a malformed result. Same shape as LML#444.
        """
        items_with_artwork = [
            (make_library_item(id=1, artist="Artist 1", title="Album 1"), None),
            (make_library_item(id=2, artist="Artist 2", title="Album 2"), None),
        ]

        apple_music = AsyncMock(spec=AppleMusicClient)
        # Item 1 raises; item 2 returns a normal match. Without a defensive
        # wrap at the call site, the gather() in enrich_artwork_results
        # cancels item 2's enrichment mid-flight and raises.
        item2_url = "https://music.apple.com/us/album/album-2/222"
        apple_music.find_track_metadata = AsyncMock(
            side_effect=[
                AttributeError("Apple returned a non-dict item"),
                AppleMusicTrackMatch(url=item2_url, artwork_url=None, release_year=None),
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
        """LML#449 + LML#450: a single Apple Music probe call that exceeds
        the call-site timeout must NOT pull the rest of the request past
        its deadline. The enrichment wraps the call in ``asyncio.wait_for``
        with a tight ceiling; on TimeoutError the item degrades to no Apple
        URL, same as the LML#444 exception path. Pins the upper bound so
        the worst-case 4×60s retry loop in LML#450 cannot block enrichment.
        Post-LML#487 the enrichment drives ``find_track_metadata`` in
        the synthesis path (no library row to project) — the timeout
        semantics are identical because both methods share the
        ``search_song`` rate-limited endpoint.
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
            return AppleMusicTrackMatch(
                url="https://music.apple.com/should-never-resolve",
                artwork_url=None,
                release_year=None,
            )

        apple_music.find_track_metadata = AsyncMock(side_effect=slow_find)

        # Patch the call-site timeout to a tiny value so the test runs in
        # milliseconds and the asyncio.wait_for ceiling fires deterministically.
        with patch("lookup.enrichment.item.apple_music_lookup_timeout_s", return_value=0.05):
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

        Mirrors the `_log_resolver_pre_pass` (lookup/artist_resolution.py) /
        `_log_album_title_fallback` (lookup/strategies/track_on_compilation.py)
        shape: project onto
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
            return AppleMusicTrackMatch(
                url="https://music.apple.com/should-never-resolve",
                artwork_url=None,
                release_year=None,
            )

        apple_music.find_track_metadata = AsyncMock(side_effect=slow_find)

        transaction = MagicMock()
        scope = MagicMock()
        scope.transaction = transaction

        with (
            patch("lookup.enrichment.item.apple_music_lookup_timeout_s", return_value=0.05),
            patch(
                "lookup.enrichment.item.sentry_sdk.get_current_scope",
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
            return AppleMusicTrackMatch(
                url="https://music.apple.com/should-never-resolve",
                artwork_url=None,
                release_year=None,
            )

        apple_music.find_track_metadata = AsyncMock(side_effect=slow_find)

        # No active transaction.
        scope = MagicMock()
        scope.transaction = None

        with (
            patch("lookup.enrichment.item.apple_music_lookup_timeout_s", return_value=0.05),
            patch(
                "lookup.enrichment.item.sentry_sdk.get_current_scope",
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
    Mint Seymali albums). Without a gate at the enrichment's
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
        # Synthesized search-URL fallback fills in instead — for the services
        # that still have one. Spotify's was deleted in LML#573 → None here.
        assert enriched.spotify_url is None
        assert enriched.youtube_music_url is not None
        assert "search" in enriched.youtube_music_url.lower()
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


class TestExternalArtworkProbe:
    """LML#487 / BS#1184: when the WXYC library catalog has no row whose title
    clears the LML#477 (PR #481) gate against the requested album, probe Apple
    Music for the literal `(artist, song, album)` and surface the matched
    response's ``artwork_url`` + ``release_year`` on the synthesized
    ``DiscogsSearchResult``.

    Concretely: a flowsheet entry for `Hebebeb (Zrag)` from Noura Mint
    Seymali's *Yenbett* — when the catalog has only her earlier *Tzenni*
    album — must surface Yenbett's cover art, not Tzenni's. Strict
    superset of LML#401 (the no-Discogs-match case still works), and
    strict no-regression on LML#477's gate (the gate still fires).

    The probe uses the SAME ``search_song`` endpoint the existing
    ``find_track_url`` calls — no new Apple Music API rounds vs. the
    LML#401 quota.
    """

    @pytest.mark.asyncio
    async def test_probe_artwork_surfaces_on_synthesized_result_when_no_discogs_match(self):
        """LML#401 superset: when ``artwork is None`` (no Discogs match for the
        requested release) AND the Apple Music probe returns a track-level
        match, the synthesized result carries ``artwork_url`` +
        ``release_year`` from that probe response. No regression vs. LML#401:
        the URL was already surfacing; the artwork + year are the new fields.
        """
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(artist="Noura Mint Seymali", title="Hebebeb (Zrag)")
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/hebebeb-zrag/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/abc/600x600bb.jpg",
            release_year=2025,
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        # Keep find_track_url defined so a refactor that accidentally calls
        # the old method instead of the new one surfaces in test output.
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Hebebeb (Zrag)",
            album="Yenbett",
            apple_music=apple_music,
        )

        _, enriched = results[0]
        assert enriched is not None
        # BS#1185 sentinel contract preserved — synthesis path is taken.
        assert enriched.release_id == 0
        assert enriched.release_url == ""
        # The new fields land on the synthesized result.
        assert enriched.artwork_url == probe_match.artwork_url
        assert enriched.release_year == 2025
        assert enriched.apple_music_url == probe_match.url

    @pytest.mark.asyncio
    async def test_probe_artwork_surfaces_when_library_row_title_misses_album(self):
        """LML#487 core: library has Tzenni; DJ requests Yenbett. The PR #481
        gate already blocks Tzenni's streaming-URL projection; this ticket
        ALSO discards Tzenni's Discogs artwork and probes Apple Music for
        Yenbett. The synthesized result carries the probe's Yenbett artwork
        — not the leaked Tzenni cover from the Discogs hit."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        # Discogs hit for the WRONG album (Tzenni). Its artwork_url and
        # release_year would leak through ``artwork.model_copy(update=...)``
        # without the LML#487 fix.
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni-WRONG.jpg",
            release_year=2014,
        )
        # Apple Music probe for `(Noura, Hebebeb, Yenbett)` returns the
        # Yenbett match (right album).
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/hebebeb-zrag/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/abc/600x600bb.jpg",
            release_year=2025,
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        # Even if get_release returns Tzenni's year, the synthesis path
        # ignores it for the synthesized-result branch.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(item, tzenni_artwork)],
            discogs_service,
            song="Hebebeb (Zrag)",
            album="Yenbett",
            apple_music=apple_music,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Tzenni's wrong artwork is gone.
        assert enriched.artwork_url != "https://example.com/tzenni-WRONG.jpg"
        # Yenbett's correct artwork (from Apple) is on the result.
        assert enriched.artwork_url == probe_match.artwork_url
        # Yenbett's release year, not Tzenni's.
        assert enriched.release_year == 2025
        # Synthesized result — release_id reset to 0 so BS#1185 still
        # branches on the no-Discogs-match write path.
        assert enriched.release_id == 0
        assert enriched.release_url == ""

    @pytest.mark.asyncio
    async def test_probe_not_invoked_when_library_row_title_matches(self):
        """Happy path: when the library row title clears the LML#477 gate,
        the synthesis path is not taken and the external album probe is
        not invoked. Pins acceptance criterion 3 — don't waste an Apple
        Music call on rows that already match.

        ``find_track_url`` (the song-level URL probe) still runs for the
        streaming-URL surfacing path; what this test pins is that
        ``find_track_metadata`` (the artwork-bearing probe) is not called
        when the gate passes."""
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1,
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            artwork_url="https://example.com/oyola.jpg",
            release_year=2015,
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock()
        apple_music.find_track_url = AsyncMock(
            return_value="https://music.apple.com/us/song/back-baby/123"
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
            song="Back, Baby",
            album="On Your Own Love Again",
            apple_music=apple_music,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Library row's Discogs artwork survives — gate passed.
        assert enriched.artwork_url == "https://example.com/oyola.jpg"
        assert enriched.release_year == 2015
        # External-artwork probe (find_track_metadata) NOT invoked: don't
        # waste an Apple Music call on a happy-path hit.
        apple_music.find_track_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_returning_none_falls_through_to_legacy_shape(self):
        """When the Apple Music probe returns None (no acceptable match —
        per the 80/80/80 floor inside ``find_track_metadata``), the
        synthesized result shape matches the existing LML#401 no-Discogs-
        match behaviour: ``artwork_url`` is None, search-URL fallbacks
        fill streaming URLs. No regression."""
        item = make_library_item(artist="Obscure Artist", title="Obscure Song")

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)
        apple_music.find_track_url = AsyncMock(return_value=None)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Obscure Song",
            album="Obscure Album",
            apple_music=apple_music,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_id == 0
        # No artwork surfaces — synthesized result with null artwork field.
        assert enriched.artwork_url is None
        assert enriched.release_year is None
        # Search-URL fallbacks still fill (legacy LML#401 contract) for YTM/BC/SC.
        # Spotify's fallback was deleted in LML#573 → None (no client/flag here).
        assert enriched.spotify_url is None
        assert enriched.youtube_music_url is not None
        assert "search" in enriched.youtube_music_url.lower()

    @pytest.mark.asyncio
    async def test_probe_not_invoked_when_album_is_none(self):
        """When no album is requested (artist+song-only lookup) AND the
        library row carries Discogs artwork, the gate trivially passes
        (no signal to gate against — preserves LML#477's
        ``test_applies_streaming_links_when_album_not_requested``
        edge case). The artwork-bearing probe must not fire — the legacy
        ``find_track_url`` is what surfaces the song URL on the
        happy-path slot.
        """
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")
        artwork = make_discogs_result(
            release_id=1,
            artist="Juana Molina",
            album="DOGA",
            artwork_url="https://example.com/doga.jpg",
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock()
        apple_music.find_track_url = AsyncMock(return_value=None)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="DOGA",
            artist="Juana Molina",
            year=2022,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="DOGA",
            album=None,
            apple_music=apple_music,
        )

        apple_music.find_track_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_skipped_on_happy_path_when_streaming_links_override_present(self):
        """When the library row clears the LML#477 gate AND has a
        librarian-curated ``streaming_links`` Apple Music override, the
        final ``apple_music_url`` is the override (per the
        ``apple_music_override or apple_music_url or None`` precedence).
        The probe's URL would be discarded — so skip the Apple Music
        call entirely. Saves one quota slot per overridden item."""
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1,
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            artwork_url="https://example.com/oyola.jpg",
            release_year=2015,
        )

        override_url = "https://music.apple.com/us/album/oyola/curated-by-librarian"
        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(return_value={"apple_music_url": override_url})

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value="https://music.apple.com/discarded")
        apple_music.find_track_metadata = AsyncMock()

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
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url == override_url
        apple_music.find_track_url.assert_not_called()
        apple_music.find_track_metadata.assert_not_called()


class TestArtistIdentitySplitGate:
    """LML#504: split the over-broad ``is_album_derived_eligible`` gate.

    ``artist_bio`` / ``wikipedia_url`` / ``profile_tokens`` flow from
    ``get_artist_details(release.artist_id)`` — strictly artist-scoped.
    The PR #500 gate (``is_top1 and library_row_acceptable``) suppressed
    them alongside the album-scoped fields whenever the library row's
    title failed the LML#477 floor — even when the artist identity was
    intact (Yenbett→Tzenni same-artist-sibling shape).

    The new ``artist_identity_verified`` predicate gates the three
    artist-scoped fields independently:

    * ``library_row_artist_verified`` — request artist matches
      ``item.artist`` at score ≥ 80 AND ``item.artist`` is not a
      compilation alias.
    * ``release_side_artist_verified`` — when ``top1_release`` was
      fetched, its artist also matches at score ≥ 80 AND is not a
      compilation alias. When ``top1_release is None`` (e.g. LML#507
      prefetch skipped, or fetch errored), only the library-row hop
      is consulted.

    ``artist_identity_verified = library_row_artist_verified AND
    (top1_release is None OR release_side_artist_verified)``.
    """

    @pytest.mark.asyncio
    async def test_synth_path_preserves_bio_when_artist_identity_intact(self):
        """Tzenni-vs-Yenbett same-artist sibling: library row title fails
        the LML#477 floor (artwork/year/etc. correctly suppressed), but
        the requested artist matches the library row's artist AND the
        top-1 release's artist. The artist-scoped fields — ``artist_bio``,
        ``wikipedia_url`` — survive the synthesis path."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni-WRONG.jpg",
            release_year=2014,
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/hebebeb-zrag/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/abc/600x600bb.jpg",
            release_year=2025,
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=99,
            label="Glitterbeat",
            genres=["African"],
            styles=["Tuareg"],
            tracklist=[TrackItem(position="1", title="Tzenni", duration="3:00", artists=[])],
            released="2014-04-29",
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Noura Mint Seymali",
            profile="Mauritanian singer.",
            image_url="https://img.discogs.com/noura.jpg",
            urls=["https://en.wikipedia.org/wiki/Noura_Mint_Seymali"],
        )

        results = await enrich_artwork_results(
            [(item, tzenni_artwork)],
            discogs_service,
            song="Hebebeb (Zrag)",
            album="Yenbett",
            artist="Noura Mint Seymali",
            apple_music=apple_music,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None

        # Artist-scoped fields preserved via library_row_artist_verified +
        # release_side_artist_verified (both hops agree on the artist).
        assert enriched.artist_bio == "Mauritanian singer."
        assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/Noura_Mint_Seymali"

        # Album-scoped fields STILL suppressed — the title fails LML#477 so
        # ``library_row_acceptable=False``. ``release_year`` falls through
        # to the Apple Music probe (2025), not the Discogs release's 2014.
        assert enriched.release_year == 2025
        assert enriched.tracklist is None
        assert enriched.label is None
        assert enriched.full_release_date is None
        # ``discogs_artist_id`` is strictly artist-scoped (= ``release.artist_id``)
        # so it surfaces alongside the bio under the new artist-identity gate.
        # API contract coherence: any response carrying ``artist_bio`` also
        # carries the cache key clients use to dedupe artist metadata.
        assert enriched.discogs_artist_id == 99
        # ``artist_image_url`` stays on the album gate (no iOS mount; surfacing
        # it on the synthesis path would be pure payload waste).
        assert enriched.artist_image_url is None

    @pytest.mark.asyncio
    async def test_release_side_drift_suppresses_bio(self):
        """Library row artist matches the request, but the top-1 Discogs
        release's artist drifts (would happen via cross-artist fuzzy
        collisions or stale cache rows). Bio must NOT surface — the
        release-side hop is what anchors the bio's provenance to the
        requested artist."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/yenbett.jpg",
            release_year=2025,
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        # Release-side artist drift — release.artist is NOT Noura.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Completely Different Artist",
            year=2014,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Completely Different Artist",
            profile="Some other bio that does NOT belong to Noura.",
            urls=["https://en.wikipedia.org/wiki/Completely_Different"],
        )

        results = await enrich_artwork_results(
            [(item, tzenni_artwork)],
            discogs_service,
            song="Hebebeb (Zrag)",
            album="Yenbett",
            artist="Noura Mint Seymali",
            apple_music=apple_music,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # release_side_artist_verified=False → suppress bio + wiki.
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None
        assert enriched.profile_tokens is None

    @pytest.mark.asyncio
    async def test_release_side_drift_suppresses_writer_credits(self):
        """BMI writer credits are *person* attribution for royalty reporting,
        so they ride the artist-identity gate, not just the album-title gate.
        When the library row's title matches the request
        (``library_row_acceptable=True``) but the top-1 release's artist drifts
        (``release_side_artist_verified=False``), the wrong artist's composers
        must NOT leak — even though album-derived fields like ``tracklist``
        still surface from the title-matched release. Regression guard for the
        worst case of a royalty-reporting feature (LML#699)."""
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )

        discogs_service = AsyncMock()
        # Release-side artist drift — release.artist is NOT Noura — but the
        # release still carries a writer-role credit that WOULD surface under
        # the album-only gate.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Completely Different Artist",
            artist_id=99,
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="1", title="Tzenni", artists=[])],
            extra_artists=[ArtistCredit(name="Some Other Writer", role="Written-By")],
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Completely Different Artist",
            profile="A bio that does NOT belong to Noura.",
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Tzenni",
            album="Tzenni",
            artist="Noura Mint Seymali",
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Artist identity failed → bio AND writer_credits suppressed...
        assert enriched.artist_bio is None
        assert enriched.writer_credits is None
        # ...while the album-title match still surfaces album-derived fields,
        # proving writer_credits follows the artist gate, not the album gate.
        assert enriched.tracklist is not None

    @pytest.mark.asyncio
    async def test_release_side_drift_suppresses_track_level_writer_credits(self):
        """The artist-identity gate covers BOTH writer-credit precisions: even
        when the drifted release carries a per-track writer for the played
        track (which, absent the gate, would emit ``provenance="track"``), an
        artist-identity mismatch suppresses it. Locks the F1 fix against the
        track-level path added in Phase 2 (LML#699)."""
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )

        discogs_service = AsyncMock()
        # Wrong artist, but a per-track writer keyed to the played track's
        # position — the track-level path would fire if the gate let it.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Completely Different Artist",
            artist_id=99,
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="A1", title="Hebebeb (Zrag)", artists=[])],
            track_writers={"A1": [ArtistCredit(name="Wrong Track Writer", role="Written-By")]},
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99, name="Completely Different Artist"
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Hebebeb (Zrag)",
            album="Tzenni",
            artist="Noura Mint Seymali",
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Track-level writers for the played track exist, but the artist gate
        # suppresses the whole field — no wrong-artist composer leaks.
        assert enriched.writer_credits is None
        assert enriched.tracklist is not None

    @pytest.mark.asyncio
    async def test_empty_request_artist_falls_back_to_legacy_gate(self):
        """Album-only lookups (``parsed.artist=None``) — a supported path
        per the album-only fallback in ``lookup/enrichment`` — would silently lose bio under
        a strict artist-identity gate (no request anchor to score against).
        The new gate falls back to ``library_row_acceptable`` semantics
        when ``request_artist`` is empty/whitespace, preserving the legacy
        bio surfacing for album-only callers. Same fallback for ``artist=' '``
        — ``score_match`` would normalize the whitespace away and score
        ``100.0`` against anything (``normalize_for_comparison`` strips
        whitespace before fuzzy compare), so the bool-of-stripped guard is
        the load-bearing check that catches both cases."""
        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        for empty_artist in ("", " ", None):
            results = await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                song="French Disko",
                album="Aluminum Tunes",
                artist=empty_artist,
                extended=True,
            )

            _, enriched = results[0]
            assert enriched is not None
            # library_row_acceptable=True (artwork + title match), so the
            # legacy gate surfaces bio. The split gate falls back here
            # because there's no request artist to anchor identity against.
            assert enriched.artist_bio == "French-British band.", (
                f"Empty request_artist={empty_artist!r} should fall back to "
                f"legacy gate, surfacing bio"
            )
            assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/Stereolab"

    @pytest.mark.asyncio
    async def test_compilation_alias_on_item_artist_suppresses_bio(self):
        """When the library row's ``item.artist`` is a compilation alias
        (``is_compilation_artist`` returns True), there's no coherent
        artist identity to anchor the bio to — even when score_match
        clears the floor against the request."""
        item = make_library_item(id=42, artist="Various Artists", title="Trojan Box Set: Ska")
        artwork = make_discogs_result(
            release_id=1,
            artist="Various Artists",
            album="Trojan Box Set: Ska",
            artwork_url="https://example.com/trojan.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Trojan Box Set: Ska",
            artist="Various Artists",
            year=2002,
            artist_id=194,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=194,
            name="Various",
            profile="V/A blurb.",
            urls=["https://en.wikipedia.org/wiki/Various_artists"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Skinhead Moonstomp",
            album="Trojan Box Set: Ska",
            artist="Various Artists",
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # V/A library row → library_row_artist_verified=False → suppress.
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_compilation_alias_on_release_artist_suppresses_bio(self):
        """When the top-1 release's artist is a compilation alias
        (``release.artist`` reads "Various", "V/A", etc.), the bio is
        the V/A grab-bag blurb — not an artist's biography. Suppress."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Augustus Pablo", title="King Tubby Meets Rockers")
        # Synthesis path: artwork present but title misses.
        wrong_artwork = make_discogs_result(
            release_id=1,
            artist="Augustus Pablo",
            album="King Tubby Meets Rockers",
            artwork_url="https://example.com/wrong.jpg",
        )

        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/right.jpg",
            release_year=1979,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        # Release.artist is V/A — release_side_artist_verified must fail.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="King Tubby Meets Rockers",
            artist="Various",
            year=1976,
            artist_id=194,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=194,
            name="Various",
            profile="V/A grab-bag blurb that doesn't belong to Augustus Pablo.",
            urls=["https://en.wikipedia.org/wiki/V/A"],
        )

        results = await enrich_artwork_results(
            [(item, wrong_artwork)],
            discogs_service,
            song="Cassava Piece",
            album="Some Different Album",
            artist="Augustus Pablo",
            apple_music=apple_music,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_extended_profile_tokens_gated_on_artist_identity(self):
        """``profile_tokens`` is parsed from ``top1_bio`` — same artist-
        scoped source as ``artist_bio`` — so it must follow the same
        gate. With the artist verified, profile_tokens populate on the
        synthesis path (where they were previously over-suppressed)."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        wrong_artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/right.jpg",
            release_year=2010,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British avant-pop group.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        results = await enrich_artwork_results(
            [(item, wrong_artwork)],
            discogs_service,
            song="Olv 26",
            album="Some Different Album",
            artist="Stereolab",
            apple_music=apple_music,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Bio + tokens preserved on synth path because artist identity verified.
        assert enriched.artist_bio == "French-British avant-pop group."
        assert enriched.profile_tokens is not None
        # Album-derived fields still suppressed.
        assert enriched.tracklist is None
        assert enriched.label is None

    @pytest.mark.asyncio
    async def test_env_flag_off_falls_back_to_album_gate(self, monkeypatch):
        """``LML_ARTIST_IDENTITY_SPLIT_GATE=false`` reverts the split:
        bio + wiki re-gate on the (old, broader) ``is_album_derived_eligible``
        path. Emergency rollback in case the split leaks somewhere
        unexpected in production."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        monkeypatch.setenv("LML_ARTIST_IDENTITY_SPLIT_GATE", "false")

        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/yenbett.jpg",
            release_year=2025,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Noura Mint Seymali",
            profile="Mauritanian singer.",
            urls=["https://en.wikipedia.org/wiki/Noura_Mint_Seymali"],
        )

        results = await enrich_artwork_results(
            [(item, tzenni_artwork)],
            discogs_service,
            song="Hebebeb (Zrag)",
            album="Yenbett",
            artist="Noura Mint Seymali",
            apple_music=apple_music,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # With flag off, bio + wiki are suppressed (legacy over-suppression).
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_breadcrumb_on_gate_divergence(self):
        """When ``artist_identity_verified != library_row_acceptable``,
        emit a Sentry breadcrumb so the rollout can be monitored for
        divergence cases (mostly: bio-recovered synth-path hits)."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/yenbett.jpg",
            release_year=2025,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Noura Mint Seymali",
            profile="Mauritanian singer.",
            urls=["https://en.wikipedia.org/wiki/Noura_Mint_Seymali"],
        )

        with patch("lookup.artist_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            await enrich_artwork_results(
                [(item, tzenni_artwork)],
                discogs_service,
                song="Hebebeb (Zrag)",
                album="Yenbett",
                artist="Noura Mint Seymali",
                apple_music=apple_music,
                extended=True,
            )

        divergence_calls = [
            c
            for c in mock_breadcrumb.call_args_list
            if c.kwargs.get("category") == "artist_identity_split_gate"
        ]
        assert len(divergence_calls) == 1, "Expected one divergence breadcrumb on synth-recovery"
        data = divergence_calls[0].kwargs["data"]
        # The two terminal verdicts: legacy gate (library_row_acceptable)
        # and new gate (artist_identity_verified). Their XOR is the
        # divergence signal — kept as derivable from these two rather than
        # carried as redundant payload fields.
        assert data["library_row_acceptable"] is False
        assert data["artist_identity_verified"] is True
        assert data["use_split_gate"] is True
        # Diagnostic flags explain how each side reached its decision.
        assert data["library_row_artist_verified"] is True
        assert data["release_side_artist_verified"] is True
        assert data["release_anchor_present"] is True

    @pytest.mark.asyncio
    async def test_split_release_featured_artist_bio_suppressed(self):
        """Split-release shape: ``release.artist_id`` defaults to the
        first-listed credit (``discogs/service.py:760-761``), so a request
        for the FEATURED artist would otherwise silently surface the LEAD
        artist's bio. The composite predicate's release-side check —
        ``score_match(request_artist, top1_release.artist)`` against the
        same first-listed credit's NAME — catches this naturally: the
        score fails the 80 floor, ``release_side_artist_verified=False``,
        bio suppressed. The issue's "known limitation, ship as xfail"
        framing pre-dated the release-side hop; with both hops in the
        composite, the leak is closed."""
        item = make_library_item(
            id=42,
            artist="Featured Artist",
            title="Split EP",
        )
        # Library row's ``item.artist`` is the featured artist (the
        # _fuzzy_search side); the Discogs hit's first-listed credit is
        # the lead artist, which is what ``release.artist`` resolves to.
        artwork = make_discogs_result(
            release_id=1,
            artist="Lead Artist",
            album="Split EP",
            artwork_url="https://example.com/split.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Split EP",
            artist="Lead Artist",  # First-listed credit, not the featured one
            year=2020,
            artist_id=11,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=11,
            name="Lead Artist",
            profile="Lead Artist bio — does NOT belong to Featured Artist.",
            urls=["https://en.wikipedia.org/wiki/Lead_Artist"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Side A",
            album="Split EP",
            artist="Featured Artist",
            extended=True,
        )

        _, enriched = results[0]
        # Bio for the wrong (lead) artist does NOT leak through.
        assert enriched.artist_bio is None
        assert enriched.wikipedia_url is None

    @pytest.mark.asyncio
    async def test_discogs_disambig_suffix_does_not_suppress_bio(self):
        """Discogs assigns ``(N)`` suffixes to common-name artists (e.g.
        ``Sessa (2)``, ``Stereolab (UK)``). ``score_match("Sessa",
        "Sessa (2)") = 71.43`` — below the 80 floor. Without the helper's
        disambiguation strip, bio would silently suppress for legitimate
        matches on every common-name request. Verifies the predicate
        strips the suffix before scoring."""
        item = make_library_item(id=42, artist="Sessa", title="Pequena Vertigem de Amor")
        artwork = make_discogs_result(
            release_id=1,
            artist="Sessa",
            album="Pequena Vertigem de Amor",
            artwork_url="https://example.com/sessa.jpg",
        )

        discogs_service = AsyncMock()
        # Discogs cache returns the disambiguated form.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Pequena Vertigem de Amor",
            artist="Sessa (2)",
            year=2024,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Sessa (2)",
            profile="Brazilian singer-songwriter.",
            urls=["https://en.wikipedia.org/wiki/Sessa_musician"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Pele de Joy",
            album="Pequena Vertigem de Amor",
            artist="Sessa",
            extended=True,
        )

        _, enriched = results[0]
        # Bio MUST surface — the disambig strip lets `Sessa` ≡ `Sessa (2)`.
        assert enriched.artist_bio == "Brazilian singer-songwriter."
        assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/Sessa_musician"

    @pytest.mark.asyncio
    async def test_alternate_artist_name_match_surfaces_bio(self):
        """`artist_matches_item` (lookup/matching.py) consults BOTH
        ``item.artist`` and ``item.alternate_artist_name``. The gate
        must mirror that — a row filed by catalogers as 'Black Dog
        Productions' with alternate_artist_name='The Black Dog' would
        score 64.71 on item.artist alone but should still surface bio
        when the request matches the alternate name."""
        item = make_library_item(
            id=42,
            artist="Black Dog Productions",
            title="Spanners",
            alternate_artist_name="The Black Dog",
        )
        artwork = make_discogs_result(
            release_id=1,
            artist="The Black Dog",
            album="Spanners",
            artwork_url="https://example.com/spanners.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Spanners",
            artist="The Black Dog",
            year=1995,
            artist_id=77,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=77,
            name="The Black Dog",
            profile="UK IDM trio.",
            urls=["https://en.wikipedia.org/wiki/The_Black_Dog"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Bolt 4",
            album="Spanners",
            artist="The Black Dog",
            extended=True,
        )

        _, enriched = results[0]
        # alternate_artist_name match anchors the gate; bio surfaces.
        assert enriched.artist_bio == "UK IDM trio."
        assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/The_Black_Dog"

    @pytest.mark.asyncio
    async def test_empty_release_artist_falls_through_to_library_row_hop(self):
        """When ``top1_release`` is non-None but ``release.artist`` is
        empty/whitespace (corrupted bulk-import row, partial Discogs
        payload), treat the release hop as "nothing to verify against"
        and fall through to library-row-only verification. Otherwise the
        predicate would require release verification it can't perform,
        suppressing bio on otherwise-valid hits."""
        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        # Release fetched but artist string is empty (corrupted cache).
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="",  # empty — would otherwise force release-side verification
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="French Disko",
            album="Aluminum Tunes",
            artist="Stereolab",
            extended=True,
        )

        _, enriched = results[0]
        # Library-row hop is verified; release hop falls through. Bio surfaces.
        assert enriched.artist_bio == "French-British band."

    @pytest.mark.asyncio
    async def test_apple_probe_artwork_suppressed_on_artist_collision(self):
        """LML#487's Apple Music probe runs with `row_artist =
        item.alternate_artist_name or item.artist`. On a fuzzy-collision
        library row whose artist disagrees with the request, the probe
        returns the WRONG artist's artwork. The new gate suppresses
        ``artwork_url`` on the synthesized result when
        ``library_row_artist_verified=False`` — gated on the same
        ``use_split_gate`` predicate as bio/wiki so the rollback flag and
        ``extended`` rollout scope apply uniformly across all LML#504 gating.
        Non-extended callers and operators flipping the rollback flag get
        legacy LML#487 artwork behavior back."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        # Library row that collided on song title — different artist than the request.
        item = make_library_item(
            id=42,
            artist="Wrong Artist",
            title="Different Album",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/wrong-artwork/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/WRONG/600x600bb.jpg",
            release_year=2025,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = None  # synth path

        results = await enrich_artwork_results(
            [(item, None)],  # no artwork — synth path
            discogs_service,
            song="Some Song",
            album="Some Album",
            artist="Right Artist",  # disagrees with item.artist
            apple_music=apple_music,
            extended=True,  # LML#504 gating is opt-in via extended
        )

        _, enriched = results[0]
        assert enriched is not None
        # apple_music_url itself still surfaces (it's a streaming link, not
        # an identity claim about the artist) — only artwork_url is gated.
        assert enriched.apple_music_url == probe_match.url
        # The wrong-artist artwork must NOT land on the response.
        assert enriched.artwork_url is None

    @pytest.mark.asyncio
    async def test_apple_probe_artwork_preserved_when_split_gate_off(self):
        """When ``extended=False`` (non-extended callers like request-o-matic
        request line / dj-site proxy) the LML#504 split gate is OFF, so
        the artwork suppression must NOT fire even on artist collisions.
        Pins the rollback contract: non-extended callers get bit-for-bit
        legacy LML#487 behavior. Same fixture as the suppression test
        above, only the extended flag differs."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        item = make_library_item(id=42, artist="Wrong Artist", title="Different Album")
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/wrong-artwork/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/WRONG/600x600bb.jpg",
            release_year=2025,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = None

        results = await enrich_artwork_results(
            [(item, None)],
            discogs_service,
            song="Some Song",
            album="Some Album",
            artist="Right Artist",
            apple_music=apple_music,
            extended=False,  # legacy gate path
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.apple_music_url == probe_match.url
        # Legacy LML#487 behaviour: probe artwork lands on the synthesized
        # result regardless of artist verification.
        assert enriched.artwork_url == probe_match.artwork_url

    @pytest.mark.asyncio
    async def test_warm_cache_skipped_when_bio_suppressed(self):
        """``_warm_bio_cache`` fires per-ref Discogs API calls (cache → API
        → write-back). When the response itself suppressed the bio, those
        calls warm refs that no client will ever read. Verify the warm task
        is gated on whether bio actually surfaced in the top-1 result."""
        # Set up the release-side-drift shape from
        # test_release_side_drift_suppresses_bio: bio gets suppressed,
        # but top1_bio is non-None (so the legacy `warm_cache and top1_bio`
        # gate would have fired).
        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        # Release-side artist drift suppresses the bio.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Completely Different Artist",
            year=1998,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Completely Different Artist",
            profile="Some other bio.",
            urls=["https://en.wikipedia.org/wiki/Other"],
        )

        with patch("lookup.enrichment.asyncio.create_task") as mock_create_task:
            await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                song="French Disko",
                album="Aluminum Tunes",
                artist="Stereolab",
                extended=True,
                warm_cache=True,
            )

        # No warm task scheduled — bio was suppressed in the response, so
        # warming its refs is pure Discogs quota burn.
        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_breadcrumb_fires_during_rollback_in_shadow_mode(self):
        """The rollback flag (``LML_ARTIST_IDENTITY_SPLIT_GATE=false``) is
        precisely when shadow-mode telemetry matters most: the team needs
        to see whether the new gate would still be diverging from the old
        if it were active, to decide if re-enablement is safe. Gating the
        breadcrumb on ``use_split_gate=True`` would destroy that signal.
        Verify breadcrumb fires regardless of the rollback flag, AND that
        ``data.use_split_gate`` reports the actual effective gate state."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        # Same setup as test_synth_path_preserves_bio: divergence shape.
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")
        tzenni_artwork = make_discogs_result(
            release_id=1,
            artist="Noura Mint Seymali",
            album="Tzenni",
            artwork_url="https://example.com/tzenni.jpg",
        )
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/x/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/yenbett.jpg",
            release_year=2025,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_match.url)
        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Tzenni",
            artist="Noura Mint Seymali",
            year=2014,
            artist_id=99,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Noura Mint Seymali",
            profile="Mauritanian singer.",
            urls=["https://en.wikipedia.org/wiki/Noura_Mint_Seymali"],
        )

        for flag_value in ("false", "0", "no", "off", "disabled"):
            with (
                patch.dict(
                    "os.environ",
                    {"LML_ARTIST_IDENTITY_SPLIT_GATE": flag_value},
                ),
                patch("lookup.artist_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb,
            ):
                await enrich_artwork_results(
                    [(item, tzenni_artwork)],
                    discogs_service,
                    song="Hebebeb (Zrag)",
                    album="Yenbett",
                    artist="Noura Mint Seymali",
                    apple_music=apple_music,
                    extended=True,
                )

            divergence_calls = [
                c
                for c in mock_breadcrumb.call_args_list
                if c.kwargs.get("category") == "artist_identity_split_gate"
            ]
            assert len(divergence_calls) == 1, (
                f"Flag={flag_value!r} should still emit breadcrumb in shadow mode"
            )
            data = divergence_calls[0].kwargs["data"]
            assert data["use_split_gate"] is False, f"Flag={flag_value!r} should disable split gate"

    @pytest.mark.asyncio
    async def test_non_top1_synth_artwork_preserved_under_per_item_gate(self):
        """Iter2 regression fix: when a non-top-1 item hits the synth path
        and its row's artist matches the request, its probe artwork must
        surface. Earlier iter1 had hoisted ``library_row_artist_verified``
        to top-1-only, so non-top-1 items unconditionally lost probe
        artwork whenever a request artist was supplied. Per-item
        computation restores legacy semantics for non-top-1 results."""
        from clients.streaming.apple_music import AppleMusicTrackMatch

        top1 = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes")
        top1_artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/top1.jpg",
        )
        # Non-top-1 item also for Stereolab, but synth-path (no Discogs
        # artwork available). Its per-item probe SHOULD surface artwork.
        non_top1 = make_library_item(id=2, artist="Stereolab", title="Dots and Loops")
        probe_match = AppleMusicTrackMatch(
            url="https://music.apple.com/us/song/non-top1-track/9",
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/dots-and-loops.jpg",
            release_year=1997,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value="https://music.apple.com/top1")

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )

        results = await enrich_artwork_results(
            [(top1, top1_artwork), (non_top1, None)],
            discogs_service,
            song="Olv 26",
            album="Aluminum Tunes",
            artist="Stereolab",
            apple_music=apple_music,
            extended=True,
        )

        _, non_top1_enriched = results[1]
        assert non_top1_enriched is not None
        # Non-top-1 item's probe artwork MUST surface (per-item verification).
        assert non_top1_enriched.artwork_url == probe_match.artwork_url

    @pytest.mark.asyncio
    async def test_broad_disambig_suffix_stripped(self):
        """Iter2: ``Stereolab (UK)`` (country-code disambig) and
        ``Sade (band)`` (single-word qualifier) need stripping too —
        the iter1 regex only handled digit-only disambig. score_match
        without strip: ``Stereolab`` vs ``Stereolab (UK)`` = 78.26 < 80.
        After the broader strip, score is 100 and bio surfaces."""
        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        # Discogs returns the country-code disambiguated form.
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab (UK)",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab (UK)",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="Olv 26",
            album="Aluminum Tunes",
            artist="Stereolab",
            extended=True,
        )

        _, enriched = results[0]
        assert enriched.artist_bio == "French-British band."

    @pytest.mark.asyncio
    async def test_empty_library_row_artist_falls_back_to_legacy_gate(self):
        """Iter2: library row with empty/None ``item.artist`` AND
        ``item.alternate_artist_name`` has no anchor to score against.
        Predicate falls back to legacy ``library_row_acceptable`` rather
        than hard-suppressing bio for what's typically a data-quality
        glitch (the row otherwise has artwork + a title match)."""
        item = make_library_item(
            id=42,
            artist=None,
            title="Aluminum Tunes",
            alternate_artist_name=None,
        )
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song="French Disko",
            album="Aluminum Tunes",
            artist="Stereolab",
            extended=True,
        )

        _, enriched = results[0]
        # Library row has no artist anchor — legacy fallback surfaces bio.
        assert enriched.artist_bio == "French-British band."

    @pytest.mark.asyncio
    async def test_breadcrumb_not_emitted_on_album_only_lookup(self):
        """Iter2 noise fix: when no request artist is supplied (album-only
        lookup), the split gate falls back to legacy and there is no
        actionable divergence even when the raw predicates differ. The
        breadcrumb must be silent on these — otherwise the dashboard
        floods on every album-only lookup with an acceptable library row."""
        item = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        artwork = make_discogs_result(
            release_id=1,
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/aluminum.jpg",
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            year=1998,
            artist_id=42,
            release_url="https://discogs.com/release/1",
        )
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=42,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
        )

        with patch("lookup.artist_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                song="French Disko",
                album="Aluminum Tunes",
                artist=None,  # album-only lookup
                extended=True,
            )

        divergence_calls = [
            c
            for c in mock_breadcrumb.call_args_list
            if c.kwargs.get("category") == "artist_identity_split_gate"
        ]
        assert len(divergence_calls) == 0, (
            "Album-only lookups must not flood the divergence breadcrumb signal"
        )


class TestSiblingOverrideProbeDivergence:
    """LML#505: post-hoc invalidation of sibling-row override URLs on the
    synthesis branch.

    The LML#477 title gate (token_sort_ratio >= 80) is permissive on
    Deluxe/Remaster/Reissue/Bonus/Limited/Expanded/Anniversary suffixes —
    ``clients/streaming/matching.score_match`` strips the parenthetical
    before scoring, so ``score_match("Album X", "Album X (Deluxe Edition)")
    == 100.0``. A library row for ``Album X`` (original release) with a
    curated ``streaming_links`` payload therefore propagates its five
    service URLs through the gate when the request is for ``Album X
    (Deluxe Edition)``. When the Discogs hit for the Deluxe is missing
    (synthesis branch — ``artwork is None``), the Apple Music probe
    (LML#487) fetches the correct Deluxe release; without LML#505 the
    override URLs from the sibling original leak through onto the
    synthesized result and the user lands on the wrong release in their
    streaming app.

    ``find_track_metadata`` enforces ``album_score >= 80`` against the
    *requested* album (``clients/streaming/apple_music.py:407-412``), so
    a probe match on the synthesis branch is, by construction, for the
    requested album whenever ``album`` was supplied. When both ``album``
    and ``item.title`` carry signal, the override URLs must therefore
    have come from a wrong-sibling row — clear them. The artist-only
    path (``album=None``) and the title-less row path (``item.title is
    None``) intentionally retain the override: in those branches the
    probe's album-score filter never ran, so a probe match does not
    imply the override is wrong.
    """

    @pytest.mark.asyncio
    async def test_deluxe_shape_clears_all_five_service_overrides_when_probe_matches(self):
        """Core bug. Library row ``Tzenni`` (sibling original) carries five
        curated streaming URLs; request is for ``Tzenni (Deluxe Edition)``
        with ``artwork=None`` (Discogs miss for the Deluxe). Apple Music
        probe finds the Deluxe. None of the five override URLs may
        propagate; ``apple_music_url`` becomes the probe URL; YouTube Music /
        Bandcamp / SoundCloud downgrade to search-URL fallbacks (not no-link).
        Spotify's templated fallback was deleted in LML#573 (the persistent
        streaming-URL cache replaces it), so with no Spotify client passed and
        its per-service flag off, ``spotify_url`` stays None here.
        """
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")

        wrong_spotify = "https://open.spotify.com/album/tzenni-original-id"
        wrong_apple = "https://music.apple.com/us/album/tzenni-original/111"
        wrong_ytm = "https://music.youtube.com/playlist?list=tzenni-original"
        wrong_bandcamp = "https://nourascuesta.bandcamp.com/album/tzenni-original"
        wrong_soundcloud = "https://soundcloud.com/noura-mint-seymali/tzenni-original"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": wrong_spotify,
                "apple_music_url": wrong_apple,
                "youtube_music_url": wrong_ytm,
                "bandcamp_url": wrong_bandcamp,
                "soundcloud_url": wrong_soundcloud,
            }
        )

        probe_url = "https://music.apple.com/us/album/tzenni-deluxe/999"
        probe_match = AppleMusicTrackMatch(
            url=probe_url,
            artwork_url="https://is1-ssl.mzstatic.com/image/thumb/deluxe/600x600bb.jpg",
            release_year=2015,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=probe_match)
        apple_music.find_track_url = AsyncMock(return_value=probe_url)

        results = await enrich_artwork_results(
            [(item, None)],  # synthesis branch — no Discogs hit
            AsyncMock(),
            song="Hebebeb (Zrag)",
            album="Tzenni (Deluxe Edition)",
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched is not None
        # None of the sibling-row override URLs propagated.
        assert enriched.spotify_url != wrong_spotify
        assert enriched.apple_music_url != wrong_apple
        assert enriched.youtube_music_url != wrong_ytm
        assert enriched.bandcamp_url != wrong_bandcamp
        assert enriched.soundcloud_url != wrong_soundcloud
        # Apple slot now carries the probe URL — Option B's free
        # precedence flip over the existing
        # ``apple_music_override or apple_music_url or None`` order.
        assert enriched.apple_music_url == probe_url
        # YouTube Music / Bandcamp / SoundCloud downgrade to search-URL
        # placeholders, NOT no-link (`_build_streaming_search_url` fills any
        # unset service when artist+song are non-empty).
        assert enriched.youtube_music_url is not None
        assert "search" in enriched.youtube_music_url.lower()
        assert enriched.bandcamp_url is not None
        assert "search" in enriched.bandcamp_url.lower()
        assert enriched.soundcloud_url is not None
        assert "search" in enriched.soundcloud_url.lower()
        # Spotify's templated fallback is gone (LML#573); nothing fills the
        # slot in this no-Spotify-client / flag-off setup.
        assert enriched.spotify_url is None

    @pytest.mark.asyncio
    async def test_probe_url_wins_precedence_over_override_on_synthesis_branch(self):
        """Focused precedence pin: synthesis branch where the override is
        set AND the probe returns a URL → the final ``apple_music_url`` is
        the probe URL, not the override. Defends Option B's side-effect
        bonus over the legacy
        ``apple_music_override or apple_music_url or None`` precedence —
        clearing the override on the synthesis branch lets the probe win.
        Option A/A+ would not earn this; they only gate whether the
        override fires.
        """
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")

        override_url = "https://music.apple.com/us/album/tzenni-original/111"
        probe_url = "https://music.apple.com/us/album/tzenni-deluxe/999"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(return_value={"apple_music_url": override_url})

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(url=probe_url, artwork_url=None, release_year=None)
        )
        apple_music.find_track_url = AsyncMock(return_value=probe_url)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Hebebeb (Zrag)",
            album="Tzenni (Deluxe Edition)",
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched.apple_music_url == probe_url

    @pytest.mark.asyncio
    async def test_happy_path_override_preserved_when_library_row_acceptable(self):
        """Regression pin across all five services. When
        ``library_row_acceptable=True`` (Discogs artwork present AND title
        clears the LML#477 gate), the override URLs MUST still propagate.
        This is the librarian-curated happy path and the suppression
        rule MUST NOT fire here. Mirrors
        ``test_apple_music_url_does_not_override_db_streaming_link`` and
        ``test_probe_skipped_on_happy_path_when_streaming_links_override_present``
        across the full five-service slot.
        """
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1,
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            artwork_url="https://example.com/oyola.jpg",
            release_year=2015,
        )

        verified_spotify = "https://open.spotify.com/album/oyola-id"
        verified_apple = "https://music.apple.com/us/album/oyola/222"
        verified_ytm = "https://music.youtube.com/playlist?list=oyola"
        verified_bandcamp = "https://jessicapratt.bandcamp.com/album/oyola"
        verified_soundcloud = "https://soundcloud.com/jessica-pratt/oyola"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": verified_spotify,
                "apple_music_url": verified_apple,
                "youtube_music_url": verified_ytm,
                "bandcamp_url": verified_bandcamp,
                "soundcloud_url": verified_soundcloud,
            }
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        # Mock both methods so a regression that flips the branch
        # surfaces in assertions/call-count rather than NameError.
        apple_music.find_track_url = AsyncMock(
            return_value="https://music.apple.com/us/song/discarded/0"
        )
        apple_music.find_track_metadata = AsyncMock()

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
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched.spotify_url == verified_spotify
        assert enriched.apple_music_url == verified_apple
        assert enriched.youtube_music_url == verified_ytm
        assert enriched.bandcamp_url == verified_bandcamp
        assert enriched.soundcloud_url == verified_soundcloud

    @pytest.mark.asyncio
    async def test_artist_only_lookup_preserves_override_on_synthesis_branch(self):
        """Artist+song-only lookup (``album=None``). The title gate falls
        through to True (no album to gate against). The override block
        fires, and synthesis runs because ``artwork=None``. The probe also
        runs with ``album=None`` — in that mode ``find_track_metadata``
        collapses to a 80/80 floor with no ``album_score`` filter
        (``clients/streaming/apple_music.py:427-436``), so a probe match
        does NOT imply the override is for the wrong album. Override
        URLs preserved.

        This test guards against a naive Option B implementation that
        suppresses purely on ``not library_row_acceptable and probe_match
        is not None`` without checking that the album-divergence signal
        was actually meaningful.
        """
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")

        override_spotify = "https://open.spotify.com/album/doga-id"
        override_apple = "https://music.apple.com/us/album/doga/777"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": override_spotify,
                "apple_music_url": override_apple,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(
                url="https://music.apple.com/us/song/doga/888",
                artwork_url=None,
                release_year=None,
            )
        )
        apple_music.find_track_url = AsyncMock(
            return_value="https://music.apple.com/us/song/doga/888"
        )

        results = await enrich_artwork_results(
            [(item, None)],  # synthesis branch
            AsyncMock(),
            song="DOGA",
            album=None,
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        # Override URLs preserved — no album-divergence signal.
        assert enriched.spotify_url == override_spotify
        # Apple slot: override beats probe per the legacy precedence
        # (no album → no Option B suppression → override stays).
        assert enriched.apple_music_url == override_apple

    @pytest.mark.asyncio
    async def test_title_less_library_row_preserves_override_on_synthesis_branch(self):
        """Library row with ``title=None``. The title gate falls through
        to True (no row title to compare). Override block fires;
        synthesis runs (``artwork=None``). Even if the probe matches,
        the library row has no title to be 'wrong' against — no signal
        that the override URLs point at a wrong sibling. Override
        preserved.

        This test guards against a naive Option B implementation that
        suppresses purely on ``not library_row_acceptable and probe_match
        is not None``; the title-less branch is a latent leak per the
        issue's open-question section but explicitly out-of-scope for
        this fix.
        """
        item = make_library_item(id=42, artist="Stereolab", title=None)

        override_spotify = "https://open.spotify.com/album/aluminum-tunes-id"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": override_spotify,
                "apple_music_url": None,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(
                url="https://music.apple.com/us/album/aluminum-tunes/999",
                artwork_url="https://example.com/aluminum-tunes.jpg",
                release_year=1998,
            )
        )
        apple_music.find_track_url = AsyncMock(return_value=None)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="French Disko",
            album="Aluminum Tunes",
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        # Override URL preserved despite probe match — no title signal.
        assert enriched.spotify_url == override_spotify

    @pytest.mark.asyncio
    async def test_synthesis_branch_probe_returns_none_preserves_override(self):
        """Synthesis branch, probe returns ``None`` (Apple Music had no
        acceptable match — the 80/80/80 floor was not cleared, or Apple
        had no record at all). No probe signal to contradict the
        override → override URLs preserved.

        Guards against an implementation that suppresses on
        ``not library_row_acceptable`` alone without checking
        ``probe_match is not None``.
        """
        item = make_library_item(id=42, artist="Noura Mint Seymali", title="Tzenni")

        override_spotify = "https://open.spotify.com/album/tzenni-original-id"
        override_apple = "https://music.apple.com/us/album/tzenni-original/111"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": override_spotify,
                "apple_music_url": override_apple,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)
        apple_music.find_track_url = AsyncMock(return_value=None)

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Hebebeb (Zrag)",
            album="Tzenni (Deluxe Edition)",
            apple_music=apple_music,
            library_db=library_db,
        )

        _, enriched = results[0]
        # Probe gave no contradicting signal — override survives.
        assert enriched.spotify_url == override_spotify
        assert enriched.apple_music_url == override_apple


class TestBandcampStreamingUrlPostprocessWiring:
    """LML#573 PR-3: the enrichment must thread the Bandcamp client into the
    streaming-URL cache post-process, AND defer Bandcamp's templated search-URL
    fallback until *after* the post-process so a resolved album page wins over
    the generic search link (the post-process only fires when the field is
    ``None``, so a pre-filled search URL would silently disable the leg)."""

    @pytest.mark.asyncio
    async def test_bandcamp_client_threaded_into_postprocess_clients(self):
        item = make_library_item(artist="Juana Molina", title="DOGA")
        bandcamp = AsyncMock()
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)
        apple_music.find_track_url = AsyncMock(return_value=None)
        spotify = AsyncMock()

        with patch(
            "lookup.enrichment.item.apply_streaming_url_postprocess",
            new=AsyncMock(),
        ) as postprocess:
            await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="la paradoja",
                album="DOGA",
                apple_music=apple_music,
                spotify=spotify,
                bandcamp=bandcamp,
            )

        postprocess.assert_awaited_once()
        clients = postprocess.await_args.kwargs["clients"]
        assert clients["bandcamp"] is bandcamp
        assert clients["apple_music"] is apple_music
        assert clients["spotify"] is spotify

    @pytest.mark.asyncio
    async def test_postprocess_sees_bandcamp_url_none_then_resolved_url_wins(self):
        # The post-process's active-filter only fires for a service whose URL
        # field is ``None``. If the templated search fallback pre-filled
        # bandcamp_url, the leg would be silently disabled — so the field MUST
        # still be None when the post-process runs. Capture it at call time to
        # pin the ordering, then resolve a real album page and assert it wins.
        item = make_library_item(artist="Juana Molina", title="DOGA")
        album_url = "https://juanamolina.bandcamp.com/album/doga"
        captured = {}

        async def _resolve(update, **kwargs):
            captured["bandcamp_url_at_call"] = update["bandcamp_url"]
            update["bandcamp_url"] = album_url

        with patch(
            "lookup.enrichment.item.apply_streaming_url_postprocess",
            new=AsyncMock(side_effect=_resolve),
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="la paradoja",
                album="DOGA",
                bandcamp=AsyncMock(),
            )

        # Ordering invariant: the search fallback had not yet run.
        assert captured["bandcamp_url_at_call"] is None
        _, enriched = results[0]
        assert enriched.bandcamp_url == album_url

    @pytest.mark.asyncio
    async def test_empty_string_streaming_link_normalized_to_none_for_postprocess(self):
        # A librarian-curated streaming_links row can carry an empty-string
        # bandcamp_url (library.db returns the column verbatim, no '' -> None
        # coercion). It must be normalized to None in the `update` dict so the
        # post-process active-filter (`is None`) and the deferred search-URL
        # fallback (`not …`) agree: an empty string must NOT silently disable
        # the cache/probe leg while still triggering the search fallback.
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")
        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": None,
                "apple_music_url": None,
                "youtube_music_url": None,
                "bandcamp_url": "",
                "soundcloud_url": None,
            }
        )
        captured = {}

        async def _capture(update, **kwargs):
            captured["bandcamp_url_at_call"] = update["bandcamp_url"]

        with patch(
            "lookup.enrichment.item.apply_streaming_url_postprocess",
            new=AsyncMock(side_effect=_capture),
        ):
            await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="la paradoja",
                album="DOGA",
                bandcamp=AsyncMock(),
                library_db=library_db,
            )

        assert captured["bandcamp_url_at_call"] is None

    @pytest.mark.asyncio
    async def test_empty_string_spotify_link_normalized_to_none(self):
        # Same empty-string asymmetry for spotify_url: it is a post-process
        # service (spotify_album) whose templated search fallback was deleted in
        # PR-1, so an un-normalized '' would skip the cache/probe leg AND surface
        # the empty string straight to the client. Normalizing to None lets the
        # post-process leg run and prevents the bare '' from leaking.
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")
        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": "",
                "apple_music_url": None,
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        # Real post-process is a no-op here (no pg / entity_store passed), so the
        # surfaced spotify_url reflects only the update-dict normalization.
        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="la paradoja",
            album="DOGA",
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched.spotify_url is None

    @pytest.mark.asyncio
    async def test_search_fallback_applies_when_postprocess_leaves_url_none(self):
        # Post-process is a no-op (flag off / cache miss / client absent); the
        # deferred search-URL fallback still fills bandcamp_url.
        item = make_library_item(artist="Juana Molina", title="DOGA")

        with patch(
            "lookup.enrichment.item.apply_streaming_url_postprocess",
            new=AsyncMock(),
        ):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="la paradoja",
                album="DOGA",
            )

        _, enriched = results[0]
        assert enriched.bandcamp_url is not None
        assert enriched.bandcamp_url.startswith("https://bandcamp.com/search?q=")
