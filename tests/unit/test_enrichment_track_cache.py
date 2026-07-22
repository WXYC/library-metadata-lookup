"""L1 cache-first Apple track probe on the /lookup happy path (LML#893).

The happy path (an acceptable library row — Discogs artwork present AND the row
title clears the LML#477 gate) probes Apple Music with ``find_track_url`` to
surface the played track's deep-link. L1 peeks a NEW track-scoped cache
(``lml_cache.track_streaming_url_cache``, keyed on
``(service, artist, album, song)``) before that ~4.8s live probe:

* HIT   -> skip the live probe, return the cached exact-track deep-link.
* MISS  -> run ``find_track_url`` live (first-lookup URL preserved) AND write
           the resolved deep-link to the TRACK cache. Never a null write.

Both the peek and the write are gated on the kill-switch flags
``lml_persist_streaming_urls`` (master) AND
``lml_persist_streaming_url_apple_music`` (per-service), so flipping either off
during an incident fully disables L1's cache read/write.

The album-keyed ``lml_cache.album_streaming_url_cache`` is NEVER written by L1
(the closed PR #898 poisoning bug). These tests pin that boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import ReleaseMetadataResponse
from entity.sources import PgSource
from entity.track_streaming_url_cache import APPLE_MUSIC_TRACK_SERVICE
from lookup.enrichment import enrich_artwork_results
from tests.factories import make_discogs_result, make_library_item

# Jessica Pratt happy-path fixture: artwork present + album == row title, so
# ``library_row_acceptable`` is True and ``find_track_url`` is the probe.
_ARTIST = "Jessica Pratt"
_ALBUM = "On Your Own Love Again"
_SONG = "Back, Baby"
_TRACK_URL = "https://music.apple.com/us/song/back-baby/123"


def _flags(*, master: bool = True, apple: bool = True) -> SimpleNamespace:
    """A Settings-like stub carrying only the flags L1 reads."""
    return SimpleNamespace(
        lml_persist_streaming_urls=master,
        lml_persist_streaming_url_apple_music=apple,
        lml_persist_streaming_url_spotify=False,
        lml_persist_streaming_url_bandcamp=False,
    )


def _happy_discogs_service() -> AsyncMock:
    svc = AsyncMock()
    svc.get_release.return_value = ReleaseMetadataResponse(
        release_id=1,
        title=_ALBUM,
        artist=_ARTIST,
        year=2015,
        artist_id=None,
        release_url="https://discogs.com/release/1",
    )
    return svc


def _happy_inputs():
    item = make_library_item(id=42, artist=_ARTIST, title=_ALBUM)
    artwork = make_discogs_result(
        release_id=1, artist=_ARTIST, album=_ALBUM, artwork_url="https://example.com/oyola.jpg"
    )
    apple_music = AsyncMock(spec=AppleMusicClient)
    apple_music.find_track_metadata = AsyncMock()
    apple_music.find_track_url = AsyncMock(return_value=_TRACK_URL)
    return item, artwork, apple_music


async def _run(apple_music, pg, item, artwork, discogs_service):
    return await enrich_artwork_results(
        [(item, artwork)],
        discogs_service,
        song=_SONG,
        album=_ALBUM,
        artist=_ARTIST,
        apple_music=apple_music,
        discogs_cache_pg=pg,
    )


@pytest.mark.asyncio
class TestL1TrackCache:
    async def test_cache_hit_skips_live_probe_and_returns_cached_url(self):
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": _TRACK_URL})

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        assert enriched.apple_music_url == _TRACK_URL
        # HIT: the ~4.8s live probe was skipped entirely.
        apple_music.find_track_url.assert_not_called()

    async def test_cache_miss_runs_live_probe_and_writes_track_cache(self):
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch(
                "lookup.enrichment.item.set_cached_track_streaming_url",
                new=AsyncMock(),
            ) as write_mock,
        ):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        # First-lookup URL is present (no #782 null) from the live probe.
        assert enriched.apple_music_url == _TRACK_URL
        apple_music.find_track_url.assert_awaited_once()
        # Write-back to the TRACK cache with the resolved deep-link.
        write_mock.assert_awaited_once()
        kwargs = write_mock.await_args.kwargs
        assert kwargs["service"] == APPLE_MUSIC_TRACK_SERVICE
        assert kwargs["song"] == _SONG
        assert kwargs["url"] == _TRACK_URL

    async def test_live_miss_returning_none_does_not_persist_null(self):
        item, artwork, apple_music = _happy_inputs()
        apple_music.find_track_url = AsyncMock(return_value=None)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch(
                "lookup.enrichment.item.set_cached_track_streaming_url",
                new=AsyncMock(),
            ) as write_mock,
        ):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        assert enriched.apple_music_url is None
        # No null persisted on a first-lookup miss (#782 / BS#1192).
        write_mock.assert_not_awaited()

    async def test_flags_off_disable_peek_and_write(self):
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": "https://music.apple.com/SHOULD-NOT-READ"})
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with (
            patch(
                "lookup.enrichment.item.get_settings",
                return_value=_flags(apple=False),
            ),
            patch(
                "lookup.enrichment.item.get_cached_track_streaming_url",
                new=AsyncMock(),
            ) as read_mock,
            patch(
                "lookup.enrichment.item.set_cached_track_streaming_url",
                new=AsyncMock(),
            ) as write_mock,
        ):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        # Flag off: the cache is not read; the live probe runs and its URL wins.
        read_mock.assert_not_awaited()
        write_mock.assert_not_awaited()
        apple_music.find_track_url.assert_awaited_once()
        assert enriched.apple_music_url == _TRACK_URL

    async def test_master_flag_off_disables_l1(self):
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)

        with (
            patch(
                "lookup.enrichment.item.get_settings",
                return_value=_flags(master=False),
            ),
            patch(
                "lookup.enrichment.item.get_cached_track_streaming_url",
                new=AsyncMock(),
            ) as read_mock,
        ):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        read_mock.assert_not_awaited()
        apple_music.find_track_url.assert_awaited_once()
        assert enriched.apple_music_url == _TRACK_URL

    async def test_l1_never_writes_the_album_cache(self):
        # Regression guard for the PR #898 poisoning bug: the album-keyed cache
        # must never be written on the L1 track path.
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # track-cache miss
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch(
                "entity.streaming_url_cache.set_cached_streaming_url",
                new=AsyncMock(),
            ) as album_write_mock,
        ):
            await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        album_write_mock.assert_not_awaited()

    async def test_track_absent_lookup_does_not_touch_track_cache(self):
        # No played track (song=None): L1 falls back to today's behavior — the
        # track cache is neither read nor written.
        item = make_library_item(id=42, artist=_ARTIST, title=_ALBUM)
        artwork = make_discogs_result(
            release_id=1, artist=_ARTIST, album=_ALBUM, artwork_url="https://example.com/o.jpg"
        )
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock()
        apple_music.find_track_url = AsyncMock(return_value=_TRACK_URL)
        pg = AsyncMock(spec=PgSource)

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch(
                "lookup.enrichment.item.get_cached_track_streaming_url",
                new=AsyncMock(),
            ) as read_mock,
            patch(
                "lookup.enrichment.item.set_cached_track_streaming_url",
                new=AsyncMock(),
            ) as write_mock,
        ):
            results = await enrich_artwork_results(
                [(item, artwork)],
                _happy_discogs_service(),
                song=None,
                album=_ALBUM,
                artist=_ARTIST,
                apple_music=apple_music,
                discogs_cache_pg=pg,
            )

        _, enriched = results[0]
        read_mock.assert_not_awaited()
        write_mock.assert_not_awaited()
        # The row title is the album, so find_track_url still runs on item.title.
        assert enriched.apple_music_url == _TRACK_URL
