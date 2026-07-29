"""Unit tests for `DELETE /admin/discogs/tombstone/{type}/{id}` (LML#510).

These exercise the router-layer plumbing — auth, error mapping, L1
eviction — against a mocked `DiscogsCacheService`. Integration coverage
against a real PG (delete actually happens; next API call re-fetches)
lives in `tests/integration/test_cache_service_tombstones.py` and
`tests/integration/test_admin_tombstone_endpoint.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_cache_service_from_pool
from routers.admin import router as admin_router

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def app_with_mocked_cache():
    """Build an isolated FastAPI app with `admin_router` mounted and the
    cache-service dependency overridden by a mock."""
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin", tags=["admin"])

    cache = AsyncMock()

    def _override_cache():
        return cache

    app.dependency_overrides[get_discogs_cache_service_from_pool] = _override_cache

    def _override_settings():
        return Settings(admin_token=ADMIN_TOKEN, _env_file=None)

    app.dependency_overrides[get_settings] = _override_settings

    return app, cache


@pytest_asyncio.fixture
async def client(app_with_mocked_cache):
    app, cache = app_with_mocked_cache
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, cache


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_authorization_returns_401(client):
    ac, _ = client
    resp = await ac.delete("/admin/discogs/tombstone/release/42")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_returns_403(client):
    ac, _ = client
    resp = await ac.delete(
        "/admin/discogs/tombstone/release/42",
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Response code mapping for the three cache_service outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_returns_200_with_body(client):
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="deleted")
    with patch("routers.admin.evict_cached", return_value=True):
        resp = await ac.delete(
            "/admin/discogs/tombstone/release/42",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "id": 42, "type": "release"}
    cache.delete_tombstone.assert_awaited_once_with("release", 42)


@pytest.mark.asyncio
async def test_exists_not_tombstone_returns_404_with_distinct_detail(client):
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="exists_not_tombstone")
    resp = await ac.delete(
        "/admin/discogs/tombstone/release/42",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert resp.status_code == 404
    # This test mounts an isolated FastAPI() with only admin_router (not
    # main.app), so the LML#771 ApiErrorResponse handlers registered on
    # main.app don't apply here — the router itself still raises the same
    # structured HTTPException; FastAPI's default `{"detail": ...}` shape is
    # what's on the wire for this isolated app.
    body = resp.json()["detail"]
    assert body["detail"] == "row exists but is not a tombstone"
    assert body["id"] == 42
    assert body["type"] == "release"


@pytest.mark.asyncio
async def test_not_found_returns_404_with_distinct_detail(client):
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="not_found")
    resp = await ac.delete(
        "/admin/discogs/tombstone/release/42",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["detail"] == "no row for this id"
    assert body["id"] == 42
    assert body["type"] == "release"


@pytest.mark.asyncio
async def test_unknown_entity_type_returns_422(client):
    ac, _ = client
    resp = await ac.delete(
        "/admin/discogs/tombstone/widget/42",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    # FastAPI Literal validation rejects unknown values with 422.
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# L1 eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_release_evicts_l1_get_release(client):
    """After deleting a tombstoned release, the L1 entry for
    `DiscogsService.get_release` must be evicted so the next request
    actually re-traverses L2/L3 rather than returning the cached `None`
    from before the tombstone was cleared."""
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="deleted")
    with patch("routers.admin.evict_cached", return_value=True) as evict:
        await ac.delete(
            "/admin/discogs/tombstone/release/42",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    # First positional arg is the decorated function we're evicting from.
    assert evict.call_count == 1
    args, _ = evict.call_args
    from discogs.service import DiscogsService

    assert args[0] is DiscogsService.get_release
    assert args[1] == 42


@pytest.mark.asyncio
async def test_deleted_artist_evicts_l1_get_artist_details(client):
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="deleted")
    with patch("routers.admin.evict_cached", return_value=True) as evict:
        await ac.delete(
            "/admin/discogs/tombstone/artist/42",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    args, _ = evict.call_args
    from discogs.service import DiscogsService

    assert args[0] is DiscogsService.get_artist_details


@pytest.mark.asyncio
async def test_l1_evict_failure_does_not_fail_request(client):
    """If the L1 eviction surface raises (e.g. a future refactor breaks the
    decorator metadata), the response stays 200 because the L2 delete
    already succeeded. The TTL will close out the L1 stale entry."""
    ac, cache = client
    cache.delete_tombstone = AsyncMock(return_value="deleted")
    with patch("routers.admin.evict_cached", side_effect=RuntimeError("boom")):
        resp = await ac.delete(
            "/admin/discogs/tombstone/release/42",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cache_service_unavailable_returns_503(client):
    """If the discogs cache pool isn't configured the dep yields None
    and the route returns 503 — same posture as the other admin
    endpoints that depend on cache services."""
    ac, _ = client
    app = ac._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_discogs_cache_service_from_pool] = lambda: None
    resp = await ac.delete(
        "/admin/discogs/tombstone/release/42",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert resp.status_code == 503
