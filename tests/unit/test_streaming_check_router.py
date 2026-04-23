"""Tests for the streaming-check router endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources
from tests.unit.conftest import override_deps


@pytest.fixture
def app_client(mock_settings):
    """FastAPI test app with streaming dependencies overridden."""
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_spotify_client: None,
            get_deezer_client: AsyncMock(),
            get_apple_music_client: None,
            get_bandcamp_client: None,
        },
    ):
        yield app


@pytest.mark.asyncio
async def test_streaming_check_success(app_client):
    """POST /api/v1/streaming-check returns 200 with valid response."""
    mock_response = StreamingCheckResponse(
        on_streaming=True,
        sources=StreamingCheckSources(
            spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0),
        ),
    )

    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["on_streaming"] is True
    assert data["sources"]["spotify"]["url"] == "https://open.spotify.com/album/abc"
    assert data["sources"]["spotify"]["confidence"] == 95.0


@pytest.mark.asyncio
async def test_streaming_check_not_found(app_client):
    """POST /api/v1/streaming-check returns on_streaming=False when no match."""
    mock_response = StreamingCheckResponse(
        on_streaming=False,
        sources=StreamingCheckSources(),
    )

    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["on_streaming"] is False


@pytest.mark.asyncio
async def test_streaming_check_missing_fields(app_client):
    """POST /api/v1/streaming-check returns 422 when required fields are missing."""
    async with AsyncClient(
        transport=ASGITransport(app=app_client), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/streaming-check", json={})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_streaming_check_internal_error(app_client):
    """POST /api/v1/streaming-check returns 500 on unexpected exception."""
    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 500
