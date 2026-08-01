"""Tests for the per-service streaming resolution status (LML#1053).

``DiscogsSearchResult.streaming_status`` disambiguates a null ``*_url``
field: ``verified`` (url present — an override / cache hit / probe hit),
``absent`` (consulted, no match — terminal, must not be re-probed),
``unresolved`` (a probe was attempted but inconclusive — timed out, the tail
was shed, or a cold cache miss — transient, may resolve on a later retry). A
service never consulted at all is represented by the ABSENCE of its key from
``streaming_status`` — never by ``absent``. See LML#1053 / BS#1819.

Additive only: every case here also pins that the underlying ``*_url``
field is computed exactly as it was before this producer existed — the
status annotates, it never changes, the URL.

The Apple leg in ``lookup/enrichment/item.py`` (``enrich_one``) already
distinguishes cache-hit / override / probe-hit (verified), a completed
no-match probe (absent), and a timeout / swallowed error (unresolved) —
this file pins that classification onto ``streaming_status.apple_music``.
Spotify/Bandcamp have no independent probe in ``enrich_one`` — only the
librarian ``library_db.streaming_links`` override; their genuine
consult+verdict is ``apply_streaming_url_postprocess``'s job (covered at the
unit level in ``test_lookup_streaming_url_postprocess.py``), so this file
only pins the ``verified`` case item.py itself can produce, plus the
never-consulted omission and the "a search-URL fallback is not a genuine
resolution" guard.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from discogs.models import ReleaseMetadataResponse
from entity.sources import PgSource
from generated.api_models import StreamingResolutionStatus
from lookup.enrichment import enrich_artwork_results
from tests.factories import make_discogs_result, make_library_item


class TestAppleStreamingStatus:
    @pytest.mark.asyncio
    async def test_probe_hit_is_verified(self):
        item = make_library_item(artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1, artist="Jessica Pratt", album="On Your Own Love Again"
        )
        apple_url = "https://music.apple.com/us/song/back-baby/123"
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_url = AsyncMock(return_value=apple_url)

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
        # The URL output is unchanged from the pre-#1053 contract...
        assert enriched.apple_music_url == apple_url
        # ...and the new status annotates it as a genuine hit.
        assert enriched.streaming_status is not None
        assert enriched.streaming_status.apple_music == StreamingResolutionStatus.verified

    @pytest.mark.asyncio
    async def test_completed_probe_with_no_match_is_absent(self):
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
        _, enriched = results[0]
        assert enriched.apple_music_url is None
        assert enriched.streaming_status is not None
        assert enriched.streaming_status.apple_music == StreamingResolutionStatus.absent

    @pytest.mark.asyncio
    async def test_shed_timed_out_probe_is_unresolved(self):
        item = make_library_item(artist="Mūm", title="Summer Make Good")
        apple_music = AsyncMock(spec=AppleMusicClient)

        async def slow_find(*_args, **_kwargs):
            await asyncio.sleep(60)
            return AppleMusicTrackMatch(
                url="https://music.apple.com/should-never-resolve",
                artwork_url=None,
                release_year=None,
                album_verified=True,
            )

        apple_music.find_track_metadata = AsyncMock(side_effect=slow_find)

        with patch("lookup.enrichment.item.apple_music_lookup_timeout_s", return_value=0.05):
            results = await enrich_artwork_results(
                [(item, None)],
                AsyncMock(),
                song="Song",
                album="Album",
                apple_music=apple_music,
            )
        _, enriched = results[0]
        assert enriched.apple_music_url is None
        assert enriched.streaming_status is not None
        assert enriched.streaming_status.apple_music == StreamingResolutionStatus.unresolved

    @pytest.mark.asyncio
    async def test_swallowed_exception_is_unresolved(self):
        # Mirrors the LML#444 degrade-to-nothing path pinned in
        # test_enrichment.py::test_apple_music_client_raising_does_not_sink_whole_batch.
        item = make_library_item(artist="Artist 1", title="Album 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(
            side_effect=AttributeError("Apple returned a non-dict item")
        )

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="Song",
            album="Album",
            apple_music=apple_music,
        )
        _, enriched = results[0]
        assert enriched.apple_music_url is None
        assert enriched.streaming_status is not None
        assert enriched.streaming_status.apple_music == StreamingResolutionStatus.unresolved

    @pytest.mark.asyncio
    async def test_never_consulted_when_no_apple_client(self):
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="French Disko",
            album="Aluminum Tunes",
            apple_music=None,
        )
        _, enriched = results[0]
        assert enriched.apple_music_url is None
        # Nothing consulted at all on this path — no status object.
        assert enriched.streaming_status is None

    @pytest.mark.asyncio
    async def test_l1_track_cache_hit_is_verified(self):
        # LML#893 L1 cache-first Apple track probe: a HIT skips the live probe
        # entirely. Fixture shape mirrors test_enrichment_track_cache.py.
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1,
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            artwork_url="https://example.com/oyola.jpg",
        )
        cached_url = "https://music.apple.com/us/song/back-baby/123"
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock()
        apple_music.find_track_url = AsyncMock(return_value=cached_url)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": cached_url})

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1,
            title="On Your Own Love Again",
            artist="Jessica Pratt",
            year=2015,
            artist_id=None,
            release_url="https://discogs.com/release/1",
        )

        flags = SimpleNamespace(
            lml_persist_streaming_urls=True,
            lml_persist_streaming_url_apple_music=True,
            lml_persist_streaming_url_spotify=False,
            lml_persist_streaming_url_bandcamp=False,
        )
        with patch("lookup.enrichment.item.get_settings", return_value=flags):
            results = await enrich_artwork_results(
                [(item, artwork)],
                discogs_service,
                song="Back, Baby",
                album="On Your Own Love Again",
                artist="Jessica Pratt",
                apple_music=apple_music,
                discogs_cache_pg=pg,
            )

        _, enriched = results[0]
        apple_music.find_track_url.assert_not_called()  # HIT skipped the live probe
        assert enriched.apple_music_url == cached_url
        assert enriched.streaming_status.apple_music == StreamingResolutionStatus.verified


class TestSpotifyBandcampStreamingStatus:
    @pytest.mark.asyncio
    async def test_spotify_verified_via_librarian_override(self):
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        artwork = make_discogs_result(
            release_id=1, artist="Jessica Pratt", album="On Your Own Love Again"
        )
        verified_spotify = "https://open.spotify.com/album/oyola-id"

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
        assert enriched.streaming_status is not None
        assert enriched.streaming_status.spotify == StreamingResolutionStatus.verified
        # Apple/Bandcamp untouched on this path — omitted, not absent.
        assert enriched.streaming_status.apple_music is None
        assert enriched.streaming_status.bandcamp is None

    @pytest.mark.asyncio
    async def test_bandcamp_verified_via_librarian_override(self):
        item = make_library_item(id=42, artist="Juana Molina", title="DOGA")
        artwork = make_discogs_result(release_id=1, artist="Juana Molina", album="DOGA")
        verified_bandcamp = "https://juanamolina.bandcamp.com/album/doga"

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": None,
                "apple_music_url": None,
                "youtube_music_url": None,
                "bandcamp_url": verified_bandcamp,
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
            album="DOGA",
            library_db=library_db,
        )
        _, enriched = results[0]
        assert enriched.bandcamp_url == verified_bandcamp
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.verified

    @pytest.mark.asyncio
    async def test_bandcamp_search_fallback_not_marked_verified(self):
        """A generic Bandcamp search-URL fallback (no direct link resolved,
        no override, postprocess not configured) is not a genuine consult
        verdict — must stay never-consulted (key omitted), not ``verified``,
        even though ``bandcamp_url`` itself ends up non-null."""
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
            song="French Disko",
            album="Aluminum Tunes",
        )
        _, enriched = results[0]
        assert enriched.bandcamp_url is not None
        assert "search" in enriched.bandcamp_url.lower()
        assert enriched.streaming_status is None


class TestStreamingStatusOmission:
    @pytest.mark.asyncio
    async def test_no_service_consulted_yields_none_streaming_status(self):
        """Never-consulted must never surface as an object full of ``absent``
        verdicts — with nothing to report, the field itself stays ``None``."""
        item = make_library_item(artist="Stereolab", title="Aluminum Tunes")

        results = await enrich_artwork_results(
            [(item, None)],
            AsyncMock(),
        )
        _, enriched = results[0]
        assert enriched.streaming_status is None

    @pytest.mark.asyncio
    async def test_to_match_result_carries_streaming_status(self):
        """The wire-boundary conversion (``DiscogsSearchResult.to_match_result``)
        must carry ``streaming_status`` through unchanged — this is the single
        conversion both ``/lookup`` and ``/lookup/bulk`` share (via
        ``lookup/orchestrator.py::_build_result_items``), so a passing
        assertion here is what guarantees the LML#681 parity rule for this
        field without needing two separate HTTP round-trips.
        """
        from discogs.models import DiscogsSearchResult
        from generated.api_models import StreamingResolution

        result = DiscogsSearchResult(
            release_id=123,
            release_url="https://discogs.com/release/123",
            apple_music_url="https://music.apple.com/us/album/x/1",
            streaming_status=StreamingResolution(apple_music=StreamingResolutionStatus.verified),
        )
        match = result.to_match_result()
        assert match.streaming_status is not None
        assert match.streaming_status.apple_music == StreamingResolutionStatus.verified
