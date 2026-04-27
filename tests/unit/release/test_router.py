"""HTTP-layer tests for POST /api/v1/releases/resolve."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from generated.api_models import (
    DiscogsLabelCredit,
    DiscogsReleaseMetadata,
)
from release.dependencies import get_resolver_dependencies
from release.orchestrator import _ResolverDependencies
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources
from tests.unit.conftest import override_deps


def _release(**overrides) -> DiscogsReleaseMetadata:
    base = {
        "release_id": 12345,
        "title": "DOGA",
        "artist": "Juana Molina",
        "year": 2024,
        "label": "Sonamos",
        "artist_id": 999,
        "labels": [DiscogsLabelCredit(label_id=42, name="Sonamos", catno="SON-001")],
        "release_url": "https://www.discogs.com/release/12345",
    }
    base.update(overrides)
    return DiscogsReleaseMetadata.model_validate(base)


def _deps_with_discogs(release):
    """Build a _ResolverDependencies whose Discogs service returns the given release."""
    discogs = AsyncMock()
    discogs.get_release.return_value = release
    return _ResolverDependencies(
        discogs=discogs,
        bandcamp=MagicMock(),
        spotify=None,
        deezer=None,
        apple_music=None,
        entity_store=None,
    )


class TestResolveEndpoint:
    @pytest.mark.asyncio
    async def test_validation_error_when_neither_url_nor_explicit(self, mock_settings):
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/releases/resolve", json={})

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unrecognized_url_returns_200_with_warning(self, mock_settings):
        from main import app

        with override_deps(
            app,
            {get_resolver_dependencies: _deps_with_discogs(release=None)},
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/releases/resolve",
                    json={"url": "https://example.com/foo"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "unknown"
        assert body["canonical"]["artist"] == ""
        assert any("recognize the URL" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_discogs_release_url_returns_canonical_metadata(self, mock_settings, monkeypatch):
        from main import app

        # Stub the streaming fan-out so this test exercises only the router + orchestrator,
        # not the streaming clients.
        async def fake_check(*args, **kwargs):
            return StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())

        monkeypatch.setattr("release.orchestrator.check_streaming_availability", fake_check)

        with override_deps(app, {get_resolver_dependencies: _deps_with_discogs(_release())}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/releases/resolve",
                    json={"url": "https://www.discogs.com/release/12345"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "discogs_release"
        assert body["source_id"] == "12345"
        assert body["canonical"]["artist"] == "Juana Molina"
        assert body["canonical"]["title"] == "DOGA"
        assert body["canonical"]["label"] == "Sonamos"
        assert body["canonical"]["catno"] == "SON-001"
        assert body["identifiers"]["discogs_release_id"] == 12345

    @pytest.mark.asyncio
    async def test_explicit_source_and_id(self, mock_settings, monkeypatch):
        from main import app

        async def fake_check(*args, **kwargs):
            return StreamingCheckResponse(
                on_streaming=True,
                sources=StreamingCheckSources(
                    spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0)
                ),
            )

        monkeypatch.setattr("release.orchestrator.check_streaming_availability", fake_check)

        with override_deps(app, {get_resolver_dependencies: _deps_with_discogs(_release())}):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/releases/resolve",
                    json={"source": "discogs_release", "id": "12345"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "discogs_release"
        assert body["streaming"]["on_streaming"] is True
        assert body["identifiers"]["spotify_album_id"] == "abc"
