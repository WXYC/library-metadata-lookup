"""Unit tests for discogs/router.py."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from discogs.models import (
    ArtistDetails,
    ArtistRef,
    DiscogsSearchResponse,
    DiscogsSearchResult,
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


class TestSearchReleasesRemoved:
    """The legacy POST /discogs/search endpoint remains removed.

    Replaced by GET /discogs/resolve-release (see TestResolveRelease below).
    Kept as a guard against accidental re-introduction of the POST shape.
    """

    @pytest.mark.asyncio
    async def test_post_endpoint_returns_404(self, app_with_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/discogs/search",
                json={"artist": "Artist", "album": "Album"},
            )

        assert resp.status_code == 404 or resp.status_code == 405


# ---------------------------------------------------------------------------
# GET /discogs/resolve-release
# ---------------------------------------------------------------------------


class TestResolveRelease:
    """Tests for GET /api/v1/discogs/resolve-release.

    Replaces the removed POST /api/v1/discogs/search for the (artist, album)
    -> release_id resolution case used by tubafrenzy's library release
    tracklist page (WXYC/tubafrenzy#546, WXYC/library-metadata-lookup#315).
    """

    @pytest.mark.asyncio
    async def test_returns_top_result(self, app_with_discogs, mock_discogs):
        mock_discogs.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Disco Not Disco, Vol. 2",
                        artist="Various",
                        release_id=12345,
                        release_url="https://www.discogs.com/release/12345",
                        confidence=0.91,
                    ),
                    DiscogsSearchResult(
                        album="Disco Not Disco",
                        artist="Various",
                        release_id=999,
                        release_url="https://www.discogs.com/release/999",
                        confidence=0.62,
                    ),
                ],
                total=2,
                cached=True,
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release",
                params={"album": "Disco Not Disco, Vol. 2", "artist": "Various"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["release_id"] == 12345
        assert data["title"] == "Disco Not Disco, Vol. 2"
        assert data["artist"] == "Various"
        assert data["release_url"] == "https://www.discogs.com/release/12345"
        assert data["confidence"] == pytest.approx(0.91)

    @pytest.mark.asyncio
    async def test_empty_results_returns_404(self, app_with_discogs, mock_discogs):
        mock_discogs.search = AsyncMock(
            return_value=DiscogsSearchResponse(results=[], total=0, cached=False)
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release",
                params={"album": "Nonexistent Album", "artist": "Nobody"},
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_album_returns_422(self, app_with_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release", params={"artist": "Stereolab"}
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release",
                params={"album": "Some Album"},
            )

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_artist_optional(self, app_with_discogs, mock_discogs):
        """Artist is optional: tubafrenzy omits it for Various Artists releases."""
        mock_discogs.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Disco Not Disco, Vol. 2",
                        artist="Various",
                        release_id=12345,
                        release_url="https://www.discogs.com/release/12345",
                        confidence=0.85,
                    ),
                ],
                total=1,
                cached=True,
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release",
                params={"album": "Disco Not Disco, Vol. 2"},
            )

        assert resp.status_code == 200
        assert resp.json()["release_id"] == 12345
        # The DiscogsSearchRequest passed to .search() should have artist=None.
        call_args = mock_discogs.search.call_args
        request = call_args.args[0] if call_args.args else call_args.kwargs["request"]
        assert request.artist is None
        assert request.album == "Disco Not Disco, Vol. 2"

    @pytest.mark.asyncio
    async def test_limit_passed_through(self, app_with_discogs, mock_discogs):
        mock_discogs.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        release_id=1,
                        release_url="https://www.discogs.com/release/1",
                        confidence=0.5,
                    )
                ],
                total=1,
                cached=True,
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/resolve-release",
                params={"album": "X", "limit": 10},
            )

        assert resp.status_code == 200
        call_kwargs = mock_discogs.search.call_args.kwargs
        assert call_kwargs.get("limit") == 10


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
