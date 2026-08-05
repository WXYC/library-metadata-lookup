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

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import ReleaseMetadataResponse
from entity.sources import PgSource
from entity.track_streaming_url_cache import APPLE_MUSIC_TRACK_SERVICE
from lookup.enrichment import enrich_artwork_results
from lookup.spine_deadline import SpineDeadline
from lookup.timeouts import apple_music_lookup_timeout_s
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


async def _slow_find_track_url(*_args, **_kwargs) -> str:
    """Sleeps longer than any timeout used in the clamped-timeout tests below,
    so a real ``asyncio.wait_for`` genuinely trips ``TimeoutError`` — no
    exception is mocked/injected."""
    await asyncio.sleep(0.2)
    return _TRACK_URL  # pragma: no cover - never reached under the tiny timeouts below


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
                "lookup.enrichment.apple_probe.set_cached_track_streaming_url",
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

    async def test_apple_probe_wait_for_uses_clamped_deadline_timeout(self):
        """LML#930: on a live probe the wait_for timeout comes from
        ``SpineDeadline.clamp_probe_timeout_s(base)`` — a caller with little
        budget left doesn't wait the full 4s ceiling on one item. Proven with a
        sentinel clamp return threaded through to the probe's ``wait_for``."""
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss -> live probe
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=700,
            effective_budget_ms=500,
        )
        captured_timeouts: list[float] = []
        real_wait_for = asyncio.wait_for

        async def _capture(coro, timeout):
            captured_timeouts.append(timeout)
            return await real_wait_for(coro, timeout)

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.apple_probe.set_cached_track_streaming_url", new=AsyncMock()),
            patch.object(SpineDeadline, "clamp_probe_timeout_s", return_value=0.123) as clamp,
            patch("asyncio.wait_for", side_effect=_capture),
        ):
            results = await enrich_artwork_results(
                [(item, artwork)],
                _happy_discogs_service(),
                song=_SONG,
                album=_ALBUM,
                artist=_ARTIST,
                apple_music=apple_music,
                discogs_cache_pg=pg,
                spine_deadline=deadline,
            )

        _, enriched = results[0]
        assert enriched.apple_music_url == _TRACK_URL
        # item.py invoked the clamp with the unbounded base ceiling...
        clamp.assert_called_once()
        assert clamp.call_args.args[0] == pytest.approx(apple_music_lookup_timeout_s())
        # ...and its clamped result was the timeout handed to the probe's wait_for.
        assert 0.123 in captured_timeouts

    async def test_apple_probe_unclamped_without_spine_deadline(self):
        """LML#930: with no deadline on the context (a direct
        ``enrich_artwork_results`` caller), the probe keeps its unbounded 4s
        ceiling — ``clamp_probe_timeout_s`` is never consulted."""
        item, artwork, apple_music = _happy_inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.apple_probe.set_cached_track_streaming_url", new=AsyncMock()),
            patch.object(SpineDeadline, "clamp_probe_timeout_s") as clamp,
        ):
            results = await _run(apple_music, pg, item, artwork, _happy_discogs_service())

        _, enriched = results[0]
        assert enriched.apple_music_url == _TRACK_URL
        clamp.assert_not_called()

    async def test_live_miss_returning_none_does_not_persist_null(self):
        item, artwork, apple_music = _happy_inputs()
        apple_music.find_track_url = AsyncMock(return_value=None)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch(
                "lookup.enrichment.apple_probe.set_cached_track_streaming_url",
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
                "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
                new=AsyncMock(),
            ) as read_mock,
            patch(
                "lookup.enrichment.apple_probe.set_cached_track_streaming_url",
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
                "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
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
                "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
                new=AsyncMock(),
            ) as read_mock,
            patch(
                "lookup.enrichment.apple_probe.set_cached_track_streaming_url",
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


@pytest.mark.asyncio
class TestClampedTimeoutLogDiscrimination:
    """LML#930 PR2 folded-in nit: a `wait_for` timeout self-inflicted by the
    deadline clamp (`item.py`'s `apple_probe_timeout_s < base_apple_timeout_s`)
    is not an Apple outage. It must log at DEBUG (not the legacy WARNING) and
    carry `apple_music.timeout.deadline_clamped=True` on the transaction; a
    genuine, unclamped (full-ceiling) timeout must still WARN as today.

    Both tests force a REAL `asyncio.wait_for` timeout (no mocked exception)
    by having ``find_track_url`` sleep longer than the effective timeout, so
    the assertion exercises the exact `except TimeoutError` branch a live
    deadline-pressured probe would hit.
    """

    async def test_clamped_timeout_logs_debug_and_sets_deadline_clamped_true(self):
        item, artwork, apple_music = _happy_inputs()
        apple_music.find_track_url = AsyncMock(side_effect=_slow_find_track_url)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss -> live probe

        deadline = SpineDeadline(
            start=time.monotonic(), hard_cap_ms=25000, caller_budget_ms=700, effective_budget_ms=500
        )
        txn = MagicMock()
        mock_logger = MagicMock()

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.apple_probe.logger", mock_logger),
            patch.object(SpineDeadline, "clamp_probe_timeout_s", return_value=0.02),
            patch(
                "lookup.enrichment.apple_probe.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=txn),
            ),
        ):
            results = await enrich_artwork_results(
                [(item, artwork)],
                _happy_discogs_service(),
                song=_SONG,
                album=_ALBUM,
                artist=_ARTIST,
                apple_music=apple_music,
                discogs_cache_pg=pg,
                spine_deadline=deadline,
            )

        _, enriched = results[0]
        assert enriched.apple_music_url is None  # timed out, no URL resolved

        # No legacy WARNING for a deadline-clamped timeout.
        for call in mock_logger.warning.call_args_list:
            assert "timed out" not in call.args[0]
        # A DEBUG line was emitted instead, naming the clamp.
        debug_messages = [call.args[0] for call in mock_logger.debug.call_args_list]
        assert any("timed out" in msg and "deadline-clamped" in msg for msg in debug_messages)

        data = {c.args[0]: c.args[1] for c in txn.set_data.call_args_list}
        assert data["apple_music.find_track_url.timeout"] is True
        assert data["apple_music.timeout.deadline_clamped"] is True
        # The clamp magnitude is recorded so a DEBUG-demoted clamped timeout
        # stays diagnosable (near-zero here == self-inflicted deadline pressure).
        assert data["apple_music.timeout.probe_timeout_s"] == 0.02

    async def test_unclamped_timeout_still_warns(self):
        """Control: with no spine deadline, the probe's timeout equals the
        (tiny, patched) base ceiling exactly — not clamped BELOW it — so a
        genuine timeout still logs the legacy WARNING."""
        item, artwork, apple_music = _happy_inputs()
        apple_music.find_track_url = AsyncMock(side_effect=_slow_find_track_url)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss -> live probe

        txn = MagicMock()
        mock_logger = MagicMock()

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.apple_probe.logger", mock_logger),
            patch("lookup.enrichment.apple_probe.apple_music_lookup_timeout_s", return_value=0.02),
            patch(
                "lookup.enrichment.apple_probe.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=txn),
            ),
        ):
            results = await enrich_artwork_results(
                [(item, artwork)],
                _happy_discogs_service(),
                song=_SONG,
                album=_ALBUM,
                artist=_ARTIST,
                apple_music=apple_music,
                discogs_cache_pg=pg,
                # No spine_deadline -> apple_probe_timeout_s == base, unclamped.
            )

        _, enriched = results[0]
        assert enriched.apple_music_url is None

        warning_messages = [call.args[0] for call in mock_logger.warning.call_args_list]
        assert any("timed out" in msg for msg in warning_messages)
        mock_logger.debug.assert_not_called()

        data = {c.args[0]: c.args[1] for c in txn.set_data.call_args_list}
        assert data["apple_music.timeout.deadline_clamped"] is False
        # Unclamped: the recorded magnitude equals the (patched) base ceiling.
        assert data["apple_music.timeout.probe_timeout_s"] == 0.02
