"""Unit tests for `POST /api/v1/cache/refresh-for-identities` router-level gating.

The endpoint's full dispatch behavior (real ``EntityStore``, PG substrate) is
covered in ``tests/integration/test_cache_refresh_for_identities.py`` (pg
marker). This module pins router-level concurrency behavior with the store
and dispatcher mocked at the DI seam — no PG required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from cache.models import CacheRefreshResultItem
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_entity_store():
    return AsyncMock()


@pytest.fixture
def app_client(mock_settings, mock_entity_store):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service
    from identity.dependencies import get_entity_store
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_entity_store: mock_entity_store,
            get_discogs_service: AsyncMock(),
        },
    ):
        yield app


class TestCacheRefreshGlobalBound:
    @pytest.mark.asyncio
    async def test_concurrent_requests_share_the_global_bound(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """Two concurrent refresh batches never exceed LML_BULK_GLOBAL_MAX_CONCURRENT (LML#716).

        Per-request knob wide (10), global knob 2, two 4-id batches → peak
        in-flight ``refresh_identity`` calls <= 2, and every id still
        completes (queue-don't-shed).
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")

        ids = [101, 102, 103, 104]
        mock_entity_store.get_release_identity_provenance_bulk.return_value = {
            i: [("discogs_release", str(i))] for i in ids
        }

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_refresh(*, identity_id, **kwargs):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return CacheRefreshResultItem(identity_id=identity_id, status="warmed", sources=None)

        with patch("cache.router.refresh_identity", new=fake_refresh):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                body = {"identity_ids": ids}
                resp_a, resp_b = await asyncio.gather(
                    ac.post("/api/v1/cache/refresh-for-identities", json=body),
                    ac.post("/api/v1/cache/refresh-for-identities", json=body),
                )

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert [r["status"] for r in resp_a.json()["results"]] == ["warmed"] * 4
        assert [r["status"] for r in resp_b.json()["results"]] == ["warmed"] * 4
        assert peak <= 2, (
            f"Peak cross-request concurrency was {peak}; global permit did not bound it"
        )
