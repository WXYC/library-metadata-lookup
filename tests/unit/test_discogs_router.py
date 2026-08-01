"""Unit tests for discogs/router.py."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from discogs.models import (
    ArtistDetails,
    ArtistRef,
    EntityType,
    MasterRelease,
    MemberRef,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from discogs.router import _require_service
from discogs.service import DiscogsService
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# _require_service
# ---------------------------------------------------------------------------


class TestRequireService:
    def test_returns_service(self):
        svc = AsyncMock(spec=DiscogsService)
        assert _require_service(svc) is svc

    def test_none_raises_503(self):
        with pytest.raises(HTTPException) as exc_info:
            _require_service(None)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discogs():
    svc = AsyncMock(spec=DiscogsService)
    return svc


@pytest.fixture
def app_with_discogs(mock_discogs, mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


@pytest.fixture
def app_without_discogs(mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestTrackReleases:
    @pytest.mark.asyncio
    async def test_success(self, app_with_discogs, mock_discogs):
        mock_discogs.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(track="Song", releases=[], total=0)
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/track-releases", params={"track": "Song"})

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/track-releases", params={"track": "Song"})

        assert resp.status_code == 503


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=123,
                title="Album",
                artist="Artist",
                release_url="https://discogs.com/release/123",
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/123")

        assert resp.status_code == 200
        assert resp.json()["title"] == "Album"

    @pytest.mark.asyncio
    async def test_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/123")

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_breaker_shed_returns_503(self, app_with_discogs, mock_discogs):
        """R2-3: a saturation-breaker shed on this diagnostic route returns 503
        (transient / retryable), NOT a raw 500."""
        from discogs.breaker import DiscogsBreakerOpenError

        mock_discogs.get_release = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/123")

        assert resp.status_code == 503


class TestSearchReleasesRemoved:
    """The /discogs/search endpoint has been removed. All callers use /lookup instead."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_404(self, app_with_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/discogs/search",
                json={"artist": "Artist", "album": "Album"},
            )

        assert resp.status_code == 404 or resp.status_code == 405


# ---------------------------------------------------------------------------
# GET /discogs/artist/{artist_id}
# ---------------------------------------------------------------------------


class TestGetArtist:
    @pytest.mark.asyncio
    async def test_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_artist_details = AsyncMock(
            return_value=ArtistDetails(
                artist_id=45,
                name="Stereolab",
                profile="Anglo-French band from London",
                image_url="https://img.discogs.com/stereolab.jpg",
                name_variations=["Stéreolab"],
                aliases=[ArtistRef(id=100, name="The Groop")],
                members=[MemberRef(id=200, name="Lætitia Sadier", active=True)],
                urls=["https://en.wikipedia.org/wiki/Stereolab"],
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/artist/45")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Stereolab"
        assert data["artist_id"] == 45
        assert data["profile"] == "Anglo-French band from London"
        assert data["image_url"] == "https://img.discogs.com/stereolab.jpg"
        assert data["urls"] == ["https://en.wikipedia.org/wiki/Stereolab"]
        assert len(data["aliases"]) == 1
        assert len(data["members"]) == 1

    @pytest.mark.asyncio
    async def test_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_artist_details = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/artist/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/artist/45")

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_breaker_shed_returns_503(self, app_with_discogs, mock_discogs):
        """R2-3: a saturation-breaker shed on this route returns 503 (transient /
        retryable), NOT a raw 500. Mirrors ``TestGetRelease.test_breaker_shed_returns_503``."""
        from discogs.breaker import DiscogsBreakerOpenError

        mock_discogs.get_artist_details = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/artist/45")

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /discogs/entity/{entity_type}/{entity_id}
# ---------------------------------------------------------------------------


class TestResolveEntity:
    @pytest.mark.asyncio
    async def test_resolve_artist(self, app_with_discogs, mock_discogs):
        mock_discogs.get_artist_details = AsyncMock(
            return_value=ArtistDetails(artist_id=45, name="Stereolab")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/artist/45")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "Stereolab", "type": "artist", "id": 45}

    @pytest.mark.asyncio
    async def test_resolve_release(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=789,
                title="Aluminum Tunes",
                artist="Stereolab",
                release_url="https://www.discogs.com/release/789",
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/release/789")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "Aluminum Tunes", "type": "release", "id": 789}

    @pytest.mark.asyncio
    async def test_resolve_master(self, app_with_discogs, mock_discogs):
        mock_discogs.get_master = AsyncMock(
            return_value=MasterRelease(master_id=456, title="Dots and Loops")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/master/456")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "Dots and Loops", "type": "master", "id": 456}

    @pytest.mark.asyncio
    async def test_artist_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_artist_details = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/artist/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_release_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/release/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_release_breaker_shed_returns_503(self, app_with_discogs, mock_discogs):
        """R2-3: the entity-release branch also degrades a saturation-breaker shed
        to 503 (transient), not a raw 500."""
        from discogs.breaker import DiscogsBreakerOpenError

        mock_discogs.get_release = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/release/789")

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_master_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_master = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/master/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_entity_type(self, app_with_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/label/123")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/entity/artist/45")

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# ?refresh=true (LML#498) — L1 cache refresh path
# ---------------------------------------------------------------------------


class TestRefreshQuery:
    """`?refresh=true` on the entity GET endpoints evicts the L1 entry for that
    key BEFORE invoking the service, forcing a re-traversal through L2 (PG) and
    L3 (Discogs API). Verifies the wiring at the route seam; the eviction
    primitive itself is covered in test_memory_cache.py::TestEvictCached."""

    @pytest.mark.asyncio
    async def test_artist_refresh_true_evicts_before_service_call(
        self, app_with_discogs, mock_discogs
    ):
        mock_discogs.get_artist_details = AsyncMock(
            return_value=ArtistDetails(artist_id=45, name="Stereolab")
        )

        with patch("discogs.router.evict_cached") as mock_evict:
            mock_evict.return_value = True
            async with AsyncClient(
                transport=ASGITransport(app=app_with_discogs), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/discogs/artist/45?refresh=true")

        assert resp.status_code == 200
        # Eviction was invoked against the cached method with the artist id.
        mock_evict.assert_called_once_with(DiscogsService.get_artist_details, 45)
        # The fall-through service call still happened — refresh evicts, the
        # next call repopulates.
        mock_discogs.get_artist_details.assert_awaited_once_with(45)

    @pytest.mark.asyncio
    async def test_artist_refresh_false_skips_eviction(self, app_with_discogs, mock_discogs):
        mock_discogs.get_artist_details = AsyncMock(
            return_value=ArtistDetails(artist_id=45, name="Stereolab")
        )

        with patch("discogs.router.evict_cached") as mock_evict:
            async with AsyncClient(
                transport=ASGITransport(app=app_with_discogs), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/discogs/artist/45?refresh=false")

        assert resp.status_code == 200
        mock_evict.assert_not_called()

    @pytest.mark.asyncio
    async def test_artist_refresh_absent_skips_eviction(self, app_with_discogs, mock_discogs):
        # Default of `refresh` query param is False, matching prior behavior.
        mock_discogs.get_artist_details = AsyncMock(
            return_value=ArtistDetails(artist_id=45, name="Stereolab")
        )

        with patch("discogs.router.evict_cached") as mock_evict:
            async with AsyncClient(
                transport=ASGITransport(app=app_with_discogs), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/discogs/artist/45")

        assert resp.status_code == 200
        mock_evict.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_refresh_true_evicts_before_service_call(
        self, app_with_discogs, mock_discogs
    ):
        mock_discogs.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=789,
                title="Aluminum Tunes",
                artist="Stereolab",
                release_url="https://www.discogs.com/release/789",
            )
        )

        with patch("discogs.router.evict_cached") as mock_evict:
            mock_evict.return_value = True
            async with AsyncClient(
                transport=ASGITransport(app=app_with_discogs), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/discogs/release/789?refresh=true")

        assert resp.status_code == 200
        mock_evict.assert_called_once_with(DiscogsService.get_release, 789)
        mock_discogs.get_release.assert_awaited_once_with(789)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entity_type,svc_attr,service_factory",
        [
            (
                EntityType.artist,
                "get_artist_details",
                lambda eid: ArtistDetails(artist_id=eid, name="Stereolab"),
            ),
            (
                EntityType.release,
                "get_release",
                lambda eid: ReleaseMetadataResponse(
                    release_id=eid,
                    title="Aluminum Tunes",
                    artist="Stereolab",
                    release_url=f"https://www.discogs.com/release/{eid}",
                ),
            ),
            (
                EntityType.master,
                "get_master",
                lambda eid: MasterRelease(master_id=eid, title="Dots and Loops"),
            ),
        ],
    )
    async def test_entity_refresh_true_evicts_correct_cache(
        self, app_with_discogs, mock_discogs, entity_type, svc_attr, service_factory
    ):
        # Parametrized across the full EntityType enum: if a future value is
        # added to EntityType without a matching refresh leg in the router,
        # this test grows a missing-case failure rather than silently no-op'ing.
        setattr(
            mock_discogs,
            svc_attr,
            AsyncMock(return_value=service_factory(42)),
        )

        with patch("discogs.router.evict_cached") as mock_evict:
            mock_evict.return_value = True
            async with AsyncClient(
                transport=ASGITransport(app=app_with_discogs), base_url="http://test"
            ) as client:
                resp = await client.get(
                    f"/api/v1/discogs/entity/{entity_type.value}/42?refresh=true"
                )

        assert resp.status_code == 200, resp.text
        # Eviction targeted the cache for THIS entity type's service method.
        mock_evict.assert_called_once_with(getattr(DiscogsService, svc_attr), 42)

    @pytest.mark.asyncio
    async def test_refresh_true_after_real_priming_clears_l1(self, app_with_discogs, mock_discogs):
        # End-to-end against the actual L1 cache (not mocked): prime the same
        # cache the @async_cached decorator writes to (the closure-captured
        # cache, exposed via the stashed `_lml_cache` introspection attr —
        # see test_memory_cache.py::TestEvictCached for the contract), then
        # confirm a `?refresh=true` HTTP call removes that key. Pins that the
        # router wires the right (cache, func_name) pair into `evict_cached`.
        #
        # We do not prime via `get_artist_cache()` because the autouse
        # `reset_caches` fixture nulls the lazy-init reference between tests,
        # making `get_artist_cache()` return a fresh TTLCache while the
        # decorator's closure still holds the prior instance.
        from discogs.memory_cache import make_normalized_cache_key

        artist_cache = DiscogsService.get_artist_details._lml_cache
        artist = ArtistDetails(artist_id=45, name="Stereolab")
        key = make_normalized_cache_key("get_artist_details", 45)
        artist_cache[key] = artist
        assert key in artist_cache

        mock_discogs.get_artist_details = AsyncMock(return_value=artist)
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/artist/45?refresh=true")

        assert resp.status_code == 200
        # L1 entry is gone — the next call would re-traverse.
        assert key not in artist_cache
