"""LML#1098: the synchronous, bounded, cache-first Bandcamp live probe on the
enrichment (``/lookup/bulk``) path.

Driven end-to-end through ``enrich_artwork_results`` -> ``enrich_one`` (the same
harness the Apple L1 track-cache tests use), because #1098's contract is an
observable property of an enriched result: with the flag on, a cold
``(artist, album)`` gets a DIRECT Bandcamp album URL in the same response
instead of the ``bandcamp.com/search?q=`` fallback; with the flag off, behavior
is byte-for-byte today's.

The probe reuses #1106's ``resolve_streaming_url_with_cache(fail_fast=True)``:
cache-first peek, a single breaker-gated ``find_album_match`` on a cold miss,
negative-cache on a clean no-match, and NO cache write on a shed/timeout
(``fail_fast`` re-raises before the UPSERT). One call, no title-variant
fan-out (#1094).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.bandcamp import BandcampClient, BandcampRateLimitedError
from clients.bandcamp_breaker import BandcampBreakerOpenError
from discogs.models import ReleaseMetadataResponse
from entity.sources import PgSource
from generated.api_models import StreamingResolutionStatus
from lookup.enrichment import enrich_artwork_results
from lookup.rowless import ROWLESS_LIBRARY_ID
from lookup.spine_deadline import SpineDeadline
from lookup.streaming_url_postprocess import set_suppress_streaming_warm
from streaming.models import SourceMatch
from tests.factories import make_discogs_result, make_library_item

# Nilüfer Yanya / "PAINLESS" — a real WXYC-representative album that lives on
# Bandcamp (the diacritic also exercises to_match_form normalization on the
# cache key without affecting the raw find_album_match args).
_ARTIST = "Nilüfer Yanya"
_ALBUM = "PAINLESS"
_SONG = "stabilise"
_BC_URL = "https://niluferyanya.bandcamp.com/album/painless"
_SEARCH_PREFIX = "https://bandcamp.com/search?q="


def _flags(
    *,
    live_probe: bool = True,
    master: bool = True,
    bandcamp: bool = True,
    telemetry: bool = True,
    bulk_warm: bool = False,
) -> SimpleNamespace:
    """A Settings-like stub carrying only the flags the probe + post-process read."""
    return SimpleNamespace(
        lml_persist_streaming_urls=master,
        lml_persist_streaming_url_apple_music=False,
        lml_persist_streaming_url_spotify=False,
        lml_persist_streaming_url_bandcamp=bandcamp,
        lml_bandcamp_live_probe=live_probe,
        lml_bulk_bandcamp_streaming_warm=bulk_warm,
        enable_telemetry=telemetry,
        environment="test",
    )


def _discogs_service() -> AsyncMock:
    svc = AsyncMock()
    svc.get_release.return_value = ReleaseMetadataResponse(
        release_id=1,
        title=_ALBUM,
        artist=_ARTIST,
        year=2022,
        artist_id=None,
        release_url="https://discogs.com/release/1",
    )
    return svc


def _inputs(*, match_url: str | None = _BC_URL):
    """A library-acceptable (artwork + matching title) item plus a mocked
    Bandcamp client whose ``find_album_match`` resolves ``match_url``."""
    item = make_library_item(id=42, artist=_ARTIST, title=_ALBUM)
    artwork = make_discogs_result(
        release_id=1, artist=_ARTIST, album=_ALBUM, artwork_url="https://example.com/painless.jpg"
    )
    bandcamp = AsyncMock(spec=BandcampClient)
    bandcamp.find_album_match = AsyncMock(
        return_value=SourceMatch(url=match_url, confidence=95.0) if match_url else None
    )
    return item, artwork, bandcamp


async def _run(
    bandcamp,
    pg,
    item,
    artwork,
    *,
    album: str | None = _ALBUM,
    spine_deadline=None,
    suppress: bool = True,
    entity_store=None,
):
    # The probe is BULK-path-only, gated on the warm-suppression ContextVar the
    # bulk handler sets. Tests default to the bulk context (suppress=True); pass
    # suppress=False to model the interactive /lookup path. conftest's autouse
    # fixture resets the ContextVar between tests.
    set_suppress_streaming_warm(suppress)
    return await enrich_artwork_results(
        [(item, artwork)],
        _discogs_service(),
        song=_SONG,
        album=album,
        artist=_ARTIST,
        # apple_music=None isolates the Bandcamp leg from the Apple probe;
        # entity_store=None keeps the streaming post-process inert (returns {})
        # so the only Bandcamp resolution is this ticket's inline probe — except
        # the withhold-seam test, which passes a non-None store to exercise it.
        apple_music=None,
        bandcamp=bandcamp,
        discogs_cache_pg=pg,
        entity_store=entity_store,
        spine_deadline=spine_deadline,
    )


@pytest.mark.asyncio
class TestBandcampLiveProbe:
    async def test_cache_miss_runs_live_probe_and_fills_bandcamp_url(self):
        """Cold miss -> a single fail-fast ``find_album_match`` resolves the
        DIRECT album URL, it ships in the response, and the resolution is
        written back to ``lml_cache.album_streaming_url_cache``."""
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # cache miss
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        assert enriched.bandcamp_url == _BC_URL
        bandcamp.find_album_match.assert_awaited_once_with(_ARTIST, _ALBUM, fail_fast=True)
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.verified
        # Write-back UPSERT ran (resolved URL persisted for the next play).
        assert pg.execute.await_count >= 1

    async def test_cache_hit_returns_cached_url_without_calling(self):
        """A warm cache entry short-circuits: no live call, cached URL returned."""
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": _BC_URL})  # cache hit

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        assert enriched.bandcamp_url == _BC_URL
        bandcamp.find_album_match.assert_not_awaited()
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.verified

    async def test_clean_no_match_is_absent_and_negative_cached(self):
        """A live no-match is a sourced negative (``absent``, terminal) and gets
        a null UPSERT so the next play short-circuits; the URL falls through to
        the deferred search fallback."""
        item, artwork, bandcamp = _inputs(match_url=None)  # find_album_match -> None
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss -> live probe -> None
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        bandcamp.find_album_match.assert_awaited_once()
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.absent
        assert enriched.bandcamp_url.startswith(_SEARCH_PREFIX)
        # null UPSERT recorded the sourced miss
        assert pg.execute.await_count >= 1

    async def test_flag_off_skips_probe_and_keeps_search_fallback(self):
        """Flag off: the probe never runs, no client is consulted, and the
        response keeps today's ``bandcamp.com/search?q=`` fallback (never
        surfaced as ``verified``)."""
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with patch("lookup.enrichment.item.get_settings", return_value=_flags(live_probe=False)):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        bandcamp.find_album_match.assert_not_awaited()
        assert enriched.bandcamp_url.startswith(_SEARCH_PREFIX)
        # Never consulted -> no Bandcamp verdict at all (key/field absent).
        assert enriched.streaming_status is None or enriched.streaming_status.bandcamp is None

    async def test_no_album_skips_probe(self):
        """Bandcamp deep-links are album-level: a track-only (no-album) playcut
        skips the probe (documented limitation, same as #1052's Spotify)."""
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork, album=None)

        _, _enriched = results[0]
        bandcamp.find_album_match.assert_not_awaited()

    async def test_breaker_shed_yields_unresolved_and_emits_shed_counter(self):
        """A saturation-breaker shed (``BandcampBreakerOpenError``) is transient:
        ``unresolved`` (not a sourced no-match), NO cache write, and the
        unsampled ``bandcamp_live_probe_shed`` counter fires."""
        item, artwork, bandcamp = _inputs()
        bandcamp.find_album_match = AsyncMock(side_effect=BandcampBreakerOpenError())
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # miss -> probe attempts -> sheds
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        posthog = MagicMock()

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.bandcamp_probe.get_posthog_client", return_value=posthog),
        ):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.unresolved
        assert enriched.bandcamp_url.startswith(_SEARCH_PREFIX)
        # fail_fast re-raises before the UPSERT: a shed must not poison the cache.
        pg.execute.assert_not_awaited()
        events = [c.kwargs.get("event") for c in posthog.capture.call_args_list]
        assert "bandcamp_live_probe_shed" in events

    async def test_rate_limited_shed_yields_unresolved_and_emits_counter(self):
        """A 429 that reaches the single fail-fast attempt
        (``BandcampRateLimitedError``) is a shed, not a no-match: ``unresolved``
        + shed counter, no cache write."""
        item, artwork, bandcamp = _inputs()
        bandcamp.find_album_match = AsyncMock(side_effect=BandcampRateLimitedError("429"))
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        posthog = MagicMock()

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.bandcamp_probe.get_posthog_client", return_value=posthog),
        ):
            results = await _run(bandcamp, pg, item, artwork)

        _, enriched = results[0]
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.unresolved
        pg.execute.assert_not_awaited()
        events = [c.kwargs.get("event") for c in posthog.capture.call_args_list]
        assert "bandcamp_live_probe_shed" in events

    async def test_timeout_yields_unresolved_without_shed_counter(self):
        """A ``wait_for`` timeout is transient (``unresolved``) but is NOT a
        breaker shed, so it does not emit the shed counter."""
        item, artwork, bandcamp = _inputs()

        async def _slow(*_a, **_k):
            await asyncio.sleep(0.2)
            return SourceMatch(url=_BC_URL, confidence=95.0)  # pragma: no cover

        bandcamp.find_album_match = AsyncMock(side_effect=_slow)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        posthog = MagicMock()

        # A tiny caller budget clamps the probe's wait_for near-zero -> it trips.
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=700,
            effective_budget_ms=500,
        )
        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.bandcamp_probe.get_posthog_client", return_value=posthog),
            patch.object(SpineDeadline, "clamp_probe_timeout_s", return_value=0.01),
        ):
            results = await _run(bandcamp, pg, item, artwork, spine_deadline=deadline)

        _, enriched = results[0]
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.unresolved
        pg.execute.assert_not_awaited()
        events = [c.kwargs.get("event") for c in posthog.capture.call_args_list]
        assert "bandcamp_live_probe_shed" not in events

    async def test_probe_wait_for_uses_clamped_deadline_timeout(self):
        """LML#930 parity: the probe's ``wait_for`` ceiling comes from
        ``SpineDeadline.clamp_probe_timeout_s(base)`` so a caller with little
        budget left doesn't wait the full ceiling on one item."""
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
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
            patch.object(SpineDeadline, "clamp_probe_timeout_s", return_value=0.123) as clamp,
            patch("asyncio.wait_for", side_effect=_capture),
        ):
            await _run(bandcamp, pg, item, artwork, spine_deadline=deadline)

        assert 0.123 in captured_timeouts
        clamp.assert_called()

    async def test_interactive_path_is_not_probed(self):
        """The probe is BULK-path-only. On the interactive /lookup path (warm
        NOT suppressed) it never runs — even with the flag on, the client
        injected, and a cold album — so the synchronous probe stays off the hot
        path (the #573/#651 regression guard). Without the suppression gate this
        would call ``find_album_match`` synchronously on every interactive add.
        """
        item, artwork, bandcamp = _inputs()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork, suppress=False)

        _, enriched = results[0]
        bandcamp.find_album_match.assert_not_awaited()
        assert enriched.bandcamp_url.startswith(_SEARCH_PREFIX)

    async def test_withholds_bandcamp_client_from_postprocess_when_probe_runs(self):
        """When the probe runs, ``enrich_one`` passes ``bandcamp=None`` to the
        streaming post-process so the service isn't double-handled (redundant
        warm) and its verdict isn't clobbered by ``postprocess_status``. With
        the flag off, the client flows through unchanged. Uses a non-None
        ``entity_store`` so the post-process is not the early-return no-op."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        spy = AsyncMock(return_value={})
        entity_store = MagicMock()

        # Probe on: the client is withheld (None) from the post-process.
        item, artwork, bandcamp = _inputs()
        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags()),
            patch("lookup.enrichment.item.apply_streaming_url_postprocess", spy),
        ):
            await _run(bandcamp, pg, item, artwork, entity_store=entity_store)
        assert spy.await_args.kwargs["clients"]["bandcamp"] is None

        # Probe off: the client flows through to the post-process unchanged.
        spy.reset_mock()
        item2, artwork2, bandcamp2 = _inputs()
        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags(live_probe=False)),
            patch("lookup.enrichment.item.apply_streaming_url_postprocess", spy),
        ):
            await _run(bandcamp2, pg, item2, artwork2, entity_store=entity_store)
        assert spy.await_args.kwargs["clients"]["bandcamp"] is bandcamp2

    async def test_absent_verdict_survives_a_live_postprocess(self):
        """End-to-end clobber guard: with a live post-process (non-None
        entity_store), the probe's ``absent`` verdict reaches the response —
        because the withhold keeps Bandcamp out of the post-process's consulted
        set, so ``postprocess_status`` can't override it to something else."""
        item, artwork, bandcamp = _inputs(match_url=None)  # clean no-match -> absent
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("lookup.enrichment.item.get_settings", return_value=_flags()):
            results = await _run(bandcamp, pg, item, artwork, entity_store=MagicMock())

        _, enriched = results[0]
        bandcamp.find_album_match.assert_awaited_once()
        assert enriched.streaming_status.bandcamp == StreamingResolutionStatus.absent

    async def test_both_flags_on_probe_owns_leg_and_withholds_the_1087_warm(self):
        """#1087 (bulk warm) + #1098 (probe) both on, rowless bulk item: the
        inline probe owns Bandcamp and the client is withheld from the
        post-process, so the #1087 off-path warm cannot ALSO fire ('pick one' —
        the probe wins by construction). Tested on the ABSENT case, where the
        withhold is load-bearing (a resolved URL would exclude Bandcamp from the
        post-process anyway)."""
        item = make_library_item(id=ROWLESS_LIBRARY_ID, artist=_ARTIST, title=_ALBUM)
        artwork = make_discogs_result(
            release_id=1,
            artist=_ARTIST,
            album=_ALBUM,
            artwork_url="https://example.com/painless.jpg",
        )
        bandcamp = AsyncMock(spec=BandcampClient)
        bandcamp.find_album_match = AsyncMock(return_value=None)  # miss -> absent, url stays None
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        spy = AsyncMock(return_value={})

        with (
            patch("lookup.enrichment.item.get_settings", return_value=_flags(bulk_warm=True)),
            patch("lookup.enrichment.item.apply_streaming_url_postprocess", spy),
        ):
            await _run(bandcamp, pg, item, artwork, entity_store=MagicMock())

        bandcamp.find_album_match.assert_awaited_once()  # the probe made its single call
        # withheld -> the #1087 rowless warm has no client to probe with.
        assert spy.await_args.kwargs["clients"]["bandcamp"] is None
