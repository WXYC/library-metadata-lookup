"""Integration tests for the health check endpoint."""

import pytest

pytestmark = pytest.mark.integration


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_healthy_with_db(self, app_client):
        """Health check is healthy when DB is connected."""
        resp = await app_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("healthy", "degraded")
        assert "services" in body
        assert body["services"]["database"] == "ok"

    @pytest.mark.asyncio
    async def test_response_structure(self, app_client):
        resp = await app_client.get("/health")
        body = resp.json()
        assert "status" in body
        assert "version" in body
        assert "services" in body
        assert "database" in body["services"]
        assert "discogs_api" in body["services"]
        assert "discogs_cache" in body["services"]

    @pytest.mark.asyncio
    async def test_discogs_unavailable_without_service(self, app_client):
        """Without Discogs service, those checks show 'unavailable'."""
        resp = await app_client.get("/health")
        body = resp.json()
        assert body["services"]["discogs_api"] == "unavailable"
        assert body["services"]["discogs_cache"] == "unavailable"
