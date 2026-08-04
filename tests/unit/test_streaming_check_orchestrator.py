"""Tests for the streaming-check orchestrator.

After LML#392 the orchestrator's only job is to gather over each client's
``find_album_match`` result and apply the LML#376 verdict matrix. The
per-service JSON-shape adaptation lives in each client's
``find_album_match`` override and is exercised in the client-specific test
files (test_spotify_client.py, test_deezer_client.py, test_apple_music_client.py,
test_bandcamp_client.py).
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from clients.bandcamp import BandcampTransportError
from streaming.models import SourceMatch, StreamingCheckSources
from streaming.orchestrator import (
    _BANDCAMP_STREAMING_CHECK_TIMEOUT_ENV_VAR,
    _BANDCAMP_STREAMING_CHECK_TIMEOUT_S,
    _DEFAULT_STREAMING_CHECK_TIMEOUT_S,
    _EXPECTED_SERVICE_FIELDS,
    _STREAMING_CHECK_TIMEOUT_ENV_VAR,
    _timeout_for,
    check_streaming_availability,
)


def test_orchestrator_kwargs_match_response_fields():
    """Pin the kwarg-to-response-field mapping the gather loop relies on.

    The module-load guard in `streaming/orchestrator.py` raises if these
    drift, but module-load guards don't run in test workers that mock the
    module away. This test makes the invariant CI-discoverable and survives
    any future restructuring that moves the guard.
    """
    assert set(StreamingCheckSources.model_fields) == _EXPECTED_SERVICE_FIELDS


def _mock_client(match: SourceMatch | None | Exception = None) -> AsyncMock:
    """Build a mock streaming client with a pre-set find_album_match verdict.

    A plain ``AsyncMock`` standing in for any ``BaseStreamingClient`` subclass —
    the orchestrator only depends on the ``find_album_match`` shape, so a mock
    adapter is sufficient (deletion test: no service-specific access remains).
    """
    client = AsyncMock()
    if isinstance(match, Exception):
        client.find_album_match = AsyncMock(side_effect=match)
    else:
        client.find_album_match = AsyncMock(return_value=match)
    return client


@pytest.mark.asyncio
async def test_found_on_spotify():
    """A Spotify match → on_streaming=True; result is wired to sources.spotify."""
    spotify = _mock_client(
        SourceMatch(url="https://open.spotify.com/album/abc123", confidence=95.0)
    )

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", spotify=spotify)

    assert result.on_streaming is True
    assert result.sources.spotify is not None
    assert "spotify.com" in result.sources.spotify.url
    assert result.sources.spotify.confidence == 95.0
    spotify.find_album_match.assert_awaited_once_with("Stereolab", "Aluminum Tunes")


@pytest.mark.asyncio
async def test_found_on_deezer():
    """A Deezer match → on_streaming=True; result is wired to sources.deezer."""
    deezer = _mock_client(SourceMatch(url="https://www.deezer.com/album/123", confidence=90.0))

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", deezer=deezer)

    assert result.on_streaming is True
    assert result.sources.deezer is not None
    assert "deezer.com" in result.sources.deezer.url


@pytest.mark.asyncio
async def test_found_on_apple_music():
    """An Apple Music match → on_streaming=True; result is wired to sources.apple_music."""
    apple = _mock_client(SourceMatch(url="https://music.apple.com/album/123", confidence=90.0))

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", apple_music=apple)

    assert result.on_streaming is True
    assert result.sources.apple_music is not None
    assert "apple.com" in result.sources.apple_music.url


@pytest.mark.asyncio
async def test_found_on_bandcamp():
    """A Bandcamp match → on_streaming=True; result is wired to sources.bandcamp."""
    bandcamp = _mock_client(
        SourceMatch(url="https://stereolab.bandcamp.com/album/aluminum-tunes", confidence=92.0)
    )

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", bandcamp=bandcamp)

    assert result.on_streaming is True
    assert result.sources.bandcamp is not None
    assert "bandcamp.com" in result.sources.bandcamp.url


@pytest.mark.asyncio
async def test_bandcamp_transport_failure_degrades_to_errored_not_a_hard_failure():
    """LML#1115: a Bandcamp default-mode transport failure (5xx, connect
    timeout) now raises ``BandcampTransportError`` out of ``find_album_match``
    instead of degrading to a clean ``None``. This orchestrator's existing
    generic ``except Exception`` (unchanged by #1115) already folds ANY
    client exception into ``errored_sources`` -- a degraded response, not a
    500 and not a false "not on Bandcamp" -- so this pins that the new
    exception type integrates with that pre-existing posture rather than
    escaping the gather loop or being misread as a confirmed absence.
    """
    bandcamp = _mock_client(BandcampTransportError("boom"))

    result = await check_streaming_availability("Sessa", "Estrela Acesa", bandcamp=bandcamp)

    assert result.on_streaming is None
    assert result.errored_sources == ["bandcamp"]
    assert result.sources.bandcamp is None


@pytest.mark.asyncio
async def test_not_found_on_any_service():
    """Every adapter returns None → on_streaming=False, no errored_sources."""
    spotify = _mock_client(None)
    deezer = _mock_client(None)

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is False
    assert result.sources.spotify is None
    assert result.sources.deezer is None
    # LML#376: False is reserved for "checked all, none found, none errored".
    # The empty errored_sources is part of the False contract — a regression
    # that re-introduces the bug (False with errored_sources populated) trips here.
    assert result.errored_sources == []


@pytest.mark.asyncio
async def test_no_clients_returns_inconclusive():
    """No clients dispatched → on_streaming=None with empty errored_sources."""
    result = await check_streaming_availability("Stereolab", "Aluminum Tunes")

    assert result.on_streaming is None
    # The empty-dispatched-tasks branch shares its on_streaming verdict with the
    # all-errored branch; errored_sources is what distinguishes them. Pinning []
    # here prevents a future change from conflating "nothing tried" with
    # "tried, all failed".
    assert result.errored_sources == []


@pytest.mark.asyncio
async def test_all_clients_error_returns_inconclusive():
    """Every adapter raises → on_streaming=None; errored_sources lists both, sorted."""
    spotify = _mock_client(Exception("network error"))
    deezer = _mock_client(Exception("timeout"))

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is None
    assert result.errored_sources == ["deezer", "spotify"]


@pytest.mark.asyncio
async def test_partial_error_still_returns_result():
    """One adapter errors, another matches → on_streaming=True; errored_sources records the flake."""
    spotify = _mock_client(Exception("network error"))
    deezer = _mock_client(SourceMatch(url="https://www.deezer.com/album/123", confidence=90.0))

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is True
    assert result.sources.spotify is None
    assert result.sources.deezer is not None
    # Positive evidence wins for on_streaming, but errored_sources still records
    # the failure so a caller can schedule a retry of just the spotify leg.
    assert result.errored_sources == ["spotify"]


@pytest.mark.asyncio
async def test_partial_error_with_no_match_is_inconclusive():
    """One adapter errors, the other returns no match → on_streaming=None (LML#376).

    Before LML#376's fix this collapsed to ``False``: the no-match success
    flipped ``any_checked`` to True so the verdict followed the
    "all-checked, none-found" branch and ignored the error. Persisted as
    ``library.on_streaming=false`` forever, with no retry path. After the
    fix, any-error → None so consumers' ``!== null`` guards skip the write.
    """
    spotify = _mock_client(Exception("network error"))
    deezer = _mock_client(None)

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is None
    assert result.sources.spotify is None
    assert result.sources.deezer is None
    assert "spotify" in result.errored_sources
    assert "deezer" not in result.errored_sources


@pytest.mark.asyncio
async def test_multiple_services_all_match():
    """Three adapters all match → all sources populated; on_streaming=True.

    Pins the LML#392 acceptance criterion: the orchestrator gathers over the
    ``find_album_match`` interface with no per-service branch, so multi-source
    composition is name-driven by the kwarg → field mapping only.
    """
    spotify = _mock_client(SourceMatch(url="https://open.spotify.com/album/x", confidence=95.0))
    deezer = _mock_client(SourceMatch(url="https://www.deezer.com/album/y", confidence=90.0))
    apple = _mock_client(SourceMatch(url="https://music.apple.com/album/z", confidence=88.0))

    result = await check_streaming_availability(
        "Stereolab",
        "Aluminum Tunes",
        spotify=spotify,
        deezer=deezer,
        apple_music=apple,
    )

    assert result.on_streaming is True
    assert result.sources.spotify is not None
    assert result.sources.deezer is not None
    assert result.sources.apple_music is not None


class _SlowClient:
    """A streaming client whose ``find_album_match`` sleeps before returning.

    Stands in for a Bandcamp leg stuck in a 429 backoff chain (LML#1081) — the
    orchestrator must cut it off via its per-service wall-clock ceiling rather
    than awaiting the full sleep.
    """

    def __init__(self, delay: float, match: SourceMatch | None = None):
        self._delay = delay
        self._match = match

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        await asyncio.sleep(self._delay)
        return self._match


def test_bandcamp_ceiling_looser_than_default():
    """LML#1081: Bandcamp's multi-phase, 1-req/s leg gets a looser ceiling than
    the single-call API services, but both are finite."""
    assert _BANDCAMP_STREAMING_CHECK_TIMEOUT_S > _DEFAULT_STREAMING_CHECK_TIMEOUT_S
    assert _DEFAULT_STREAMING_CHECK_TIMEOUT_S > 0


def test_timeout_for_resolves_defaults(monkeypatch):
    """LML#1081: absent an override or env var, `_timeout_for` wires Bandcamp to
    its looser ceiling and every other service to the shared default.

    Pins the resolution the async timeout tests never exercise (they all inject
    an explicit `timeouts` override): a regression that dropped the Bandcamp
    entry from `_STREAMING_CHECK_TIMEOUTS` — silently collapsing it to the 10s
    default and defeating the fix's whole point — would trip here."""
    monkeypatch.delenv(_STREAMING_CHECK_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv(_BANDCAMP_STREAMING_CHECK_TIMEOUT_ENV_VAR, raising=False)

    assert _timeout_for("bandcamp", None) == _BANDCAMP_STREAMING_CHECK_TIMEOUT_S
    assert _timeout_for("spotify", None) == _DEFAULT_STREAMING_CHECK_TIMEOUT_S
    assert _timeout_for("apple_music", None) == _DEFAULT_STREAMING_CHECK_TIMEOUT_S


def test_timeout_for_reads_env(monkeypatch):
    """LML#1081: both ceilings are tunable at request time via their env knobs
    (integer ms → seconds), no redeploy — mirrors the Apple probe convention."""
    monkeypatch.setenv(_STREAMING_CHECK_TIMEOUT_ENV_VAR, "2000")
    monkeypatch.setenv(_BANDCAMP_STREAMING_CHECK_TIMEOUT_ENV_VAR, "30000")

    assert _timeout_for("spotify", None) == 2.0
    assert _timeout_for("bandcamp", None) == 30.0


def test_timeout_for_explicit_override_beats_env(monkeypatch):
    """LML#1081: an explicit `timeouts` entry (tests / future settings-threaded
    tuning) wins over both the env knob and the static default."""
    monkeypatch.setenv(_BANDCAMP_STREAMING_CHECK_TIMEOUT_ENV_VAR, "30000")

    assert _timeout_for("bandcamp", {"bandcamp": 0.05}) == 0.05


@pytest.mark.asyncio
async def test_bandcamp_timeout_degrades_to_errored():
    """LML#1081: a Bandcamp leg that exceeds its ceiling degrades to
    ``errored_sources: ["bandcamp"]`` (LML#376 None-verdict) within a bounded
    time — it does not stall the whole request waiting out the retry chain."""
    bandcamp = _SlowClient(
        delay=5.0,
        match=SourceMatch(
            url="https://olofdreijer.bandcamp.com/album/loud-bloom", confidence=100.0
        ),
    )

    start = time.perf_counter()
    result = await check_streaming_availability(
        "Olof Dreijer",
        "Loud Bloom",
        bandcamp=bandcamp,
        timeouts={"bandcamp": 0.05},
    )
    elapsed = time.perf_counter() - start

    assert "bandcamp" in result.errored_sources
    assert result.sources.bandcamp is None
    # No positive evidence + an error → LML#376 escalates to None (do not persist).
    assert result.on_streaming is None
    # Bounded: cut at ~0.05s, not the 5s the slow leg would have taken.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_slow_bandcamp_does_not_block_other_services():
    """LML#1081: one slow service's ceiling must not delay the others — a fast
    Spotify match still wins the verdict while Bandcamp times out."""
    spotify = _mock_client(
        SourceMatch(url="https://open.spotify.com/album/2I8Y2r289lu5s26k50N9GL", confidence=95.0)
    )
    bandcamp = _SlowClient(delay=5.0)

    start = time.perf_counter()
    result = await check_streaming_availability(
        "Olof Dreijer",
        "Loud Bloom",
        spotify=spotify,
        bandcamp=bandcamp,
        timeouts={"bandcamp": 0.05},
    )
    elapsed = time.perf_counter() - start

    assert result.on_streaming is True  # positive evidence from Spotify wins
    assert result.sources.spotify is not None
    assert "bandcamp" in result.errored_sources
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_timeout_override_generalizes_to_non_bandcamp_services():
    """LML#1081: the per-service ceiling generalizes — a stalled Spotify leg is
    also cut off (here via an explicit ``timeouts`` override, proving the
    mechanism isn't Bandcamp-special) and degrades to errored. The *default*
    (un-overridden) ceiling resolution is pinned separately in
    ``test_timeout_for_resolves_defaults`` / ``test_timeout_for_reads_env``."""
    spotify = _SlowClient(delay=5.0)

    start = time.perf_counter()
    result = await check_streaming_availability(
        "Olof Dreijer",
        "Loud Bloom",
        spotify=spotify,
        timeouts={"spotify": 0.05},
    )
    elapsed = time.perf_counter() - start

    assert "spotify" in result.errored_sources
    assert result.sources.spotify is None
    assert result.on_streaming is None
    assert elapsed < 1.0
