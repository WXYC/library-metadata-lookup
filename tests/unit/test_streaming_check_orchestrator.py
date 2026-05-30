"""Tests for the streaming-check orchestrator.

After LML#392 the orchestrator's only job is to gather over each client's
``find_album_match`` result and apply the LML#376 verdict matrix. The
per-service JSON-shape adaptation lives in each client's
``find_album_match`` override and is exercised in the client-specific test
files (test_spotify_client.py, test_deezer_client.py, test_apple_music_client.py,
test_bandcamp_client.py).
"""

from unittest.mock import AsyncMock

import pytest

from streaming.models import SourceMatch
from streaming.orchestrator import check_streaming_availability


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
