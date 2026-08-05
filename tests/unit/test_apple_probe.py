"""LML#1101: direct unit surface for the inline Apple Music probe.

The probe's behavior is characterized end-to-end through ``enrich_one`` in
``tests/unit/test_enrichment.py``, ``test_enrichment_track_cache.py`` and
``test_streaming_status.py`` — those files are the net proving the LML#1101
extraction is pure motion, and none of them changed. What they cannot pin is
the extracted function's OWN contract: the ``AppleProbeResult`` tuple, which is
now the seam five values cross on their way back into ``enrich_one``
(``apple_music_url``, ``status``, ``probe_match``, ``artwork_url``,
``release_year``). A silently-dropped field there would still leave every
end-to-end test green if the caller happened not to assert on it, so it is
pinned directly here.

Mirrors ``tests/unit/test_bandcamp_live_probe.py``'s posture for the sibling
LML#1098 probe module.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clients.streaming.apple_music import AppleMusicTrackMatch
from generated.api_models import StreamingResolutionStatus
from lookup.enrichment.apple_probe import AppleProbeResult, run_apple_music_probe

VERIFIED = StreamingResolutionStatus.verified
ABSENT = StreamingResolutionStatus.absent
UNRESOLVED = StreamingResolutionStatus.unresolved

APPLE_URL = "https://music.apple.com/us/album/aluminum-tunes/1"


def _settings(**overrides):
    """A Settings stub with the two L1 kill-switches ON by default."""
    base = {
        "lml_persist_streaming_urls": True,
        "lml_persist_streaming_url_apple_music": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(*, apple_music=None, album="Aluminum Tunes", song=None, pg=None, spine_deadline=None):
    return SimpleNamespace(
        apple_music=apple_music,
        album=album,
        song=song,
        discogs_cache_pg=pg,
        spine_deadline=spine_deadline,
    )


async def _run(
    ctx,
    *,
    settings=None,
    row_artist="Stereolab",
    search_term="Aluminum Tunes",
    library_row_acceptable=True,
    apple_music_override=None,
):
    return await run_apple_music_probe(
        ctx,
        settings=settings if settings is not None else _settings(),
        row_artist=row_artist,
        search_term=search_term,
        library_row_acceptable=library_row_acceptable,
        apple_music_override=apple_music_override,
    )


class TestNeverConsulted:
    """No probe ran -> every field falsy and ``status is None`` (key omitted upstream)."""

    @pytest.mark.asyncio
    async def test_no_apple_client(self):
        assert await _run(_ctx(apple_music=None)) == AppleProbeResult(None, None, None, None, None)

    @pytest.mark.asyncio
    async def test_empty_row_artist(self):
        # enrich_one derives row_artist as ``... or ""`` — "missing" is "", not None.
        client = SimpleNamespace(find_track_url=AsyncMock())
        result = await _run(_ctx(apple_music=client), row_artist="")
        assert result.status is None
        client.find_track_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_search_term(self):
        client = SimpleNamespace(find_track_url=AsyncMock())
        result = await _run(_ctx(apple_music=client), search_term="")
        assert result.status is None
        client.find_track_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_override_short_circuits_the_happy_path_probe(self):
        # Saves one Apple quota slot: the override would win the slot anyway.
        client = SimpleNamespace(find_track_url=AsyncMock())
        result = await _run(
            _ctx(apple_music=client), library_row_acceptable=True, apple_music_override=APPLE_URL
        )
        assert result.status is None
        client.find_track_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_override_does_not_short_circuit_the_synthesis_path(self):
        # The synthesis branch needs artwork/year, which the override cannot supply.
        match = AppleMusicTrackMatch(
            url=APPLE_URL, artwork_url="https://art/1.jpg", release_year=1998, album_verified=True
        )
        client = SimpleNamespace(find_track_metadata=AsyncMock(return_value=match))
        result = await _run(
            _ctx(apple_music=client), library_row_acceptable=False, apple_music_override=APPLE_URL
        )
        assert result.status == VERIFIED
        client.find_track_metadata.assert_awaited_once()


class TestHappyPathProbe:
    """``library_row_acceptable`` -> ``find_track_url``, URL only (LML#401 baseline)."""

    @pytest.mark.asyncio
    async def test_hit_is_verified_and_carries_only_the_url(self):
        client = SimpleNamespace(find_track_url=AsyncMock(return_value=APPLE_URL))
        result = await _run(_ctx(apple_music=client))
        assert result == AppleProbeResult(APPLE_URL, VERIFIED, None, None, None)

    @pytest.mark.asyncio
    async def test_clean_no_match_is_absent_not_unresolved(self):
        client = SimpleNamespace(find_track_url=AsyncMock(return_value=None))
        result = await _run(_ctx(apple_music=client))
        assert result.status == ABSENT
        assert result.apple_music_url is None


class TestSynthesisPathProbe:
    """``not library_row_acceptable`` -> ``find_track_metadata``, artwork + year too."""

    @pytest.mark.asyncio
    async def test_match_fans_out_to_url_artwork_and_year(self):
        match = AppleMusicTrackMatch(
            url=APPLE_URL, artwork_url="https://art/1.jpg", release_year=1998, album_verified=True
        )
        client = SimpleNamespace(find_track_metadata=AsyncMock(return_value=match))
        result = await _run(_ctx(apple_music=client), library_row_acceptable=False)
        assert result == AppleProbeResult(APPLE_URL, VERIFIED, match, "https://art/1.jpg", 1998)

    @pytest.mark.asyncio
    async def test_probe_match_is_returned_for_the_lml505_sibling_gate(self):
        # enrich_one reads probe_match.album_verified to invalidate sibling-row
        # override URLs; dropping the object here would silently disable LML#505.
        match = AppleMusicTrackMatch(
            url=APPLE_URL, artwork_url=None, release_year=None, album_verified=False
        )
        client = SimpleNamespace(find_track_metadata=AsyncMock(return_value=match))
        result = await _run(_ctx(apple_music=client), library_row_acceptable=False)
        assert result.probe_match is match
        assert result.probe_match.album_verified is False

    @pytest.mark.asyncio
    async def test_no_match_is_absent_with_no_artwork_or_year(self):
        client = SimpleNamespace(find_track_metadata=AsyncMock(return_value=None))
        result = await _run(_ctx(apple_music=client), library_row_acceptable=False)
        assert result == AppleProbeResult(None, ABSENT, None, None, None)


class TestDegradePaths:
    """LML#444: a probe fault degrades to no-Apple-anything, never fails the item."""

    @pytest.mark.asyncio
    async def test_timeout_is_unresolved(self):
        client = SimpleNamespace(find_track_url=AsyncMock(side_effect=TimeoutError()))
        result = await _run(_ctx(apple_music=client))
        assert result == AppleProbeResult(None, UNRESOLVED, None, None, None)

    @pytest.mark.asyncio
    async def test_raise_is_unresolved(self):
        client = SimpleNamespace(find_track_url=AsyncMock(side_effect=RuntimeError("boom")))
        result = await _run(_ctx(apple_music=client))
        assert result == AppleProbeResult(None, UNRESOLVED, None, None, None)

    @pytest.mark.asyncio
    async def test_a_slow_probe_trips_the_ceiling_as_unresolved(self):
        async def _hang(*_a, **_k):
            await asyncio.sleep(30)

        client = SimpleNamespace(find_track_url=_hang)
        # Clamp the ceiling to near-zero through the spine deadline (LML#930).
        ctx = _ctx(
            apple_music=client,
            spine_deadline=SimpleNamespace(clamp_probe_timeout_s=lambda _base: 0.01),
        )
        result = await _run(ctx)
        assert result.status == UNRESOLVED


class TestL1TrackCache:
    """LML#893: a track-cache HIT is ``verified`` and skips the live probe."""

    @pytest.mark.asyncio
    async def test_hit_skips_the_probe(self, monkeypatch):
        client = SimpleNamespace(find_track_url=AsyncMock())
        monkeypatch.setattr(
            "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
            AsyncMock(return_value=APPLE_URL),
        )
        result = await _run(_ctx(apple_music=client, song="Pop Quiz", pg=object()))
        assert result == AppleProbeResult(APPLE_URL, VERIFIED, None, None, None)
        client.find_track_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_miss_probes_live_and_writes_back(self, monkeypatch):
        setter = AsyncMock()
        monkeypatch.setattr(
            "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr("lookup.enrichment.apple_probe.set_cached_track_streaming_url", setter)
        client = SimpleNamespace(find_track_url=AsyncMock(return_value=APPLE_URL))
        result = await _run(_ctx(apple_music=client, song="Pop Quiz", pg=object()))
        assert result.status == VERIFIED
        setter.assert_awaited_once()
        assert setter.await_args.kwargs["url"] == APPLE_URL

    @pytest.mark.asyncio
    async def test_a_null_probe_result_is_never_written_back(self, monkeypatch):
        # #782 / BS#1192: persisting a null would freeze the miss.
        setter = AsyncMock()
        monkeypatch.setattr(
            "lookup.enrichment.apple_probe.get_cached_track_streaming_url",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr("lookup.enrichment.apple_probe.set_cached_track_streaming_url", setter)
        client = SimpleNamespace(find_track_url=AsyncMock(return_value=None))
        result = await _run(_ctx(apple_music=client, song="Pop Quiz", pg=object()))
        assert result.status == ABSENT
        setter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_switch_off_skips_the_cache_peek(self, monkeypatch):
        getter = AsyncMock(return_value=APPLE_URL)
        monkeypatch.setattr("lookup.enrichment.apple_probe.get_cached_track_streaming_url", getter)
        client = SimpleNamespace(find_track_url=AsyncMock(return_value=None))
        result = await _run(
            _ctx(apple_music=client, song="Pop Quiz", pg=object()),
            settings=_settings(lml_persist_streaming_url_apple_music=False),
        )
        getter.assert_not_awaited()
        assert result.status == ABSENT
