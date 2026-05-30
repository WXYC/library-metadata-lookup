"""Tests for the streaming check orchestrator."""

from unittest.mock import AsyncMock

import pytest

from streaming.orchestrator import check_streaming_availability


def _make_spotify_result(
    artist="Stereolab", title="Aluminum Tunes", url="https://open.spotify.com/album/abc123"
):
    """Build a minimal Spotify API album result."""
    return {
        "artists": [{"name": artist}],
        "name": title,
        "external_urls": {"spotify": url},
        "id": "abc123",
    }


def _make_deezer_result(
    artist="Stereolab", title="Aluminum Tunes", url="https://www.deezer.com/album/123"
):
    """Build a minimal Deezer API album result."""
    return {
        "artist": {"name": artist},
        "title": title,
        "link": url,
    }


def _make_apple_result(
    artist="Stereolab", title="Aluminum Tunes", url="https://music.apple.com/album/123"
):
    """Build a minimal iTunes Search API album result."""
    return {
        "artistName": artist,
        "collectionName": title,
        "collectionViewUrl": url,
    }


def _make_bandcamp_artist(name="Stereolab", slug="stereolab"):
    """Build a Bandcamp autocomplete artist result."""
    return {"name": name, "url": f"https://{slug}.bandcamp.com", "slug": slug}


def _make_bandcamp_album(title="Aluminum Tunes", slug="stereolab"):
    """Build a Bandcamp catalog album entry."""
    path = title.lower().replace(" ", "-")
    return {"title": title, "url": f"https://{slug}.bandcamp.com/album/{path}"}


@pytest.mark.asyncio
async def test_found_on_spotify():
    """Returns on_streaming=True with spotify source when Spotify has a match."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(return_value=[_make_spotify_result()])

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", spotify=spotify)

    assert result.on_streaming is True
    assert result.sources.spotify is not None
    assert "spotify.com" in result.sources.spotify.url
    assert result.sources.spotify.confidence >= 80.0


@pytest.mark.asyncio
async def test_found_on_deezer():
    """Returns on_streaming=True with deezer source when Deezer has a match."""
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(return_value=[_make_deezer_result()])

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", deezer=deezer)

    assert result.on_streaming is True
    assert result.sources.deezer is not None
    assert "deezer.com" in result.sources.deezer.url


@pytest.mark.asyncio
async def test_found_on_apple_music():
    """Returns on_streaming=True with apple_music source when iTunes has a match."""
    apple = AsyncMock()
    apple.search_album = AsyncMock(return_value=[_make_apple_result()])

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", apple_music=apple)

    assert result.on_streaming is True
    assert result.sources.apple_music is not None
    assert "apple.com" in result.sources.apple_music.url


@pytest.mark.asyncio
async def test_found_on_bandcamp():
    """Returns on_streaming=True with bandcamp source when Bandcamp has a match."""
    bandcamp = AsyncMock()
    bandcamp.search_artist = AsyncMock(return_value=[_make_bandcamp_artist()])
    bandcamp.fetch_artist_catalog = AsyncMock(return_value=[_make_bandcamp_album()])

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", bandcamp=bandcamp)

    assert result.on_streaming is True
    assert result.sources.bandcamp is not None
    assert "bandcamp.com" in result.sources.bandcamp.url


@pytest.mark.asyncio
async def test_not_found_on_any_service():
    """Returns on_streaming=False when all services return no match."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(return_value=[])
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(return_value=[])

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
    """Returns on_streaming=None when no clients are provided."""
    result = await check_streaming_availability("Stereolab", "Aluminum Tunes")

    assert result.on_streaming is None
    # The empty-dispatched-tasks branch shares its on_streaming verdict with the
    # all-errored branch; errored_sources is what distinguishes them. Pinning []
    # here prevents a future change from conflating "nothing tried" with
    # "tried, all failed".
    assert result.errored_sources == []


@pytest.mark.asyncio
async def test_all_clients_error_returns_inconclusive():
    """Returns on_streaming=None when all client checks raise exceptions."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(side_effect=Exception("network error"))
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(side_effect=Exception("timeout"))

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is None
    # Both services errored — both surface in errored_sources, sorted.
    assert result.errored_sources == ["deezer", "spotify"]


@pytest.mark.asyncio
async def test_partial_error_still_returns_result():
    """One service errors, another succeeds — returns on_streaming=True."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(side_effect=Exception("network error"))
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(return_value=[_make_deezer_result()])

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
    """One service errors, another returns no match — returns on_streaming=None (LML#376).

    Before the fix this collapsed to `False`: the no-match success flipped `any_checked`
    to True so the verdict followed the "all-checked, none-found" branch and ignored the
    error. Persisted as `library.on_streaming=false` forever, with no retry path. After
    the fix, any-error → None so consumers' `!== null` guards skip the write.
    """
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(side_effect=Exception("network error"))
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(return_value=[])

    result = await check_streaming_availability(
        "Stereolab", "Aluminum Tunes", spotify=spotify, deezer=deezer
    )

    assert result.on_streaming is None
    assert result.sources.spotify is None
    assert result.sources.deezer is None
    assert "spotify" in result.errored_sources
    assert "deezer" not in result.errored_sources


@pytest.mark.asyncio
async def test_low_confidence_match_rejected():
    """A result with a poor fuzzy match is not accepted."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(
        return_value=[
            _make_spotify_result(artist="Completely Different Artist", title="Unrelated Album")
        ]
    )

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", spotify=spotify)

    assert result.on_streaming is False
    assert result.sources.spotify is None


@pytest.mark.asyncio
async def test_multiple_services_all_match():
    """All sources populated when multiple services find the album."""
    spotify = AsyncMock()
    spotify.search_album = AsyncMock(return_value=[_make_spotify_result()])
    deezer = AsyncMock()
    deezer.search_album = AsyncMock(return_value=[_make_deezer_result()])
    apple = AsyncMock()
    apple.search_album = AsyncMock(return_value=[_make_apple_result()])

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


@pytest.mark.asyncio
async def test_bandcamp_no_artist_match():
    """Returns None for bandcamp when artist autocomplete finds no match."""
    bandcamp = AsyncMock()
    bandcamp.search_artist = AsyncMock(return_value=[])

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", bandcamp=bandcamp)

    assert result.on_streaming is False
    assert result.sources.bandcamp is None


@pytest.mark.asyncio
async def test_bandcamp_artist_match_no_album():
    """Returns None for bandcamp when artist is found but album is not in catalog."""
    bandcamp = AsyncMock()
    bandcamp.search_artist = AsyncMock(return_value=[_make_bandcamp_artist()])
    bandcamp.fetch_artist_catalog = AsyncMock(
        return_value=[_make_bandcamp_album(title="Completely Different Album")]
    )

    result = await check_streaming_availability("Stereolab", "Aluminum Tunes", bandcamp=bandcamp)

    assert result.on_streaming is False
    assert result.sources.bandcamp is None
