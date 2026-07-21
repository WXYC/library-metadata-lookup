"""Unit tests for ``POST /api/v1/artists/genres/bulk`` (LML#781).

HTTP envelope: the batch/index-alignment contract, cache-hit and
cache-miss→Discogs-API-fallback wiring, the 4xx/5xx surface, and the
``LML_API_KEY`` bearer auth (like ``streaming-check`` / the sibling artists
routes). The ranking + fallback *semantics* live in
``test_artist_genre_aggregation.py``; this file pins the route.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from artists.router import _GENRES_INPUT_CAP
from config.settings import Settings, get_settings
from core.dependencies import get_discogs_cache_service_from_pool, get_discogs_service
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.models import ArtistDetails, ReleaseMetadataResponse
from discogs.service import DiscogsService
from tests.unit.conftest import override_deps

_ROUTE = "/api/v1/artists/genres/bulk"

_UNSET = object()


def _release(release_id: int, genres, styles) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=release_id,
        title=f"Release {release_id}",
        artist="x",
        release_url=f"https://www.discogs.com/release/{release_id}",
        genres=genres,
        styles=styles,
    )


@pytest.fixture
def mock_cache():
    cache = AsyncMock(spec=DiscogsCacheService)
    cache.aggregate_artist_genre_style = AsyncMock(return_value=({}, {}))
    # Bio enrichment (LML#XXX): one cache-only bulk artist-details read per batch,
    # keyed on the supplied discogs_artist_ids. Default empty → bio null everywhere
    # unless a test seeds a profile.
    cache.get_artist_details_bulk = AsyncMock(return_value={})
    return cache


@pytest.fixture
def mock_service():
    service = AsyncMock(spec=DiscogsService)
    service.search_release_ids_by_artist = AsyncMock(return_value=[])
    service.get_release = AsyncMock(return_value=None)
    return service


@pytest.fixture
def make_app(mock_settings, mock_cache, mock_service):
    """App factory: per-test control over the DI surface."""
    from main import app

    def _make(*, cache=_UNSET, service=_UNSET, settings=_UNSET):
        deps = {
            get_settings: mock_settings if settings is _UNSET else settings,
            get_discogs_cache_service_from_pool: mock_cache if cache is _UNSET else cache,
            get_discogs_service: mock_service if service is _UNSET else service,
        }
        return override_deps(app, deps), app

    return _make


async def _post(app, json_body, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(_ROUTE, json=json_body, headers=headers or {})


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_ranked_genres_and_styles(self, make_app, mock_cache):
        mock_cache.aggregate_artist_genre_style.return_value = (
            {"Rock": 4, "Electronic": 2, "Jazz": 1},
            {"Indie Rock": 3, "Ambient": 1},
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(
                app, {"artists": [{"artist_name": "Juana Molina", "discogs_artist_id": 12345}]}
            )

        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["artist_name"] == "Juana Molina"
        assert result["discogs_artist_id"] == 12345
        assert result["genres"] == ["Rock", "Electronic"]  # top-2 coarse chips
        assert result["styles"] == ["Indie Rock", "Ambient"]  # full ranked
        assert result["source"] == "cache"

    @pytest.mark.asyncio
    async def test_cache_miss_falls_back_to_discogs_api(self, make_app, mock_cache, mock_service):
        mock_cache.aggregate_artist_genre_style.return_value = ({}, {})
        mock_service.search_release_ids_by_artist.return_value = [101, 102]
        mock_service.get_release.side_effect = [
            _release(101, ["Rock", "Electronic"], ["Indie Rock"]),
            _release(102, ["Rock"], ["Post Rock"]),
        ]
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Wishy"}]})

        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["source"] == "discogs_api"
        assert result["genres"] == ["Rock", "Electronic"]
        assert result["discogs_artist_id"] is None
        # Persistence side effect: releases fetched through get_release.
        assert mock_service.get_release.await_count == 2

    @pytest.mark.asyncio
    async def test_batch_is_index_aligned(self, make_app, mock_cache):
        async def _agg(*, artist_id, artist_name):
            return ({"Rock": 1}, {}) if artist_name == "Stereolab" else ({}, {})

        mock_cache.aggregate_artist_genre_style.side_effect = _agg
        ctx, app = make_app(service=None)  # no token → misses degrade to unavailable
        with ctx:
            resp = await _post(
                app,
                {"artists": [{"artist_name": "Stereolab"}, {"artist_name": "Totally Unknown"}]},
            )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["artist_name"] for r in results] == ["Stereolab", "Totally Unknown"]
        assert results[0]["source"] == "cache"
        assert results[0]["genres"] == ["Rock"]
        assert results[1]["source"] == "unavailable"

    @pytest.mark.asyncio
    async def test_no_discogs_service_still_serves_cache_hits(self, make_app, mock_cache):
        """No token: cache path still answers; misses report unavailable, no 503."""
        mock_cache.aggregate_artist_genre_style.return_value = ({"Jazz": 2}, {})
        ctx, app = make_app(service=None)
        with ctx:
            resp = await _post(
                app, {"artists": [{"artist_name": "Duke Ellington & John Coltrane"}]}
            )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["source"] == "cache"


class TestBio:
    """Artist bio (Discogs `profile`) rides the bulk response, keyed on
    `discogs_artist_id` via one cache-only `get_artist_details_bulk` read per
    batch. Additive + nullable: name-only, uncached, blank-profile, and
    tombstone rows all report `bio: null`. The `source` enum stays genre-only.
    """

    @pytest.mark.asyncio
    async def test_cached_profile_returned_as_bio(self, make_app, mock_cache):
        mock_cache.aggregate_artist_genre_style.return_value = ({"Rock": 1}, {})
        mock_cache.get_artist_details_bulk.return_value = {
            12345: ArtistDetails(
                artist_id=12345, name="Juana Molina", profile="An Argentine musician."
            )
        }
        ctx, app = make_app()
        with ctx:
            resp = await _post(
                app, {"artists": [{"artist_name": "Juana Molina", "discogs_artist_id": 12345}]}
            )
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["bio"] == "An Argentine musician."
        # Bio provenance is independent of genre provenance — `source` unchanged.
        assert result["source"] == "cache"

    @pytest.mark.asyncio
    async def test_name_only_artist_has_null_bio(self, make_app, mock_cache):
        # No discogs_artist_id → bio can't be keyed → null.
        mock_cache.aggregate_artist_genre_style.return_value = ({"Rock": 1}, {})
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Wishy"}]})
        assert resp.status_code == 200
        assert resp.json()["results"][0]["bio"] is None

    @pytest.mark.asyncio
    async def test_uncached_artist_has_null_bio(self, make_app, mock_cache):
        # id supplied but absent from the bulk cache dict → null.
        mock_cache.get_artist_details_bulk.return_value = {}
        ctx, app = make_app(service=None)
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "X", "discogs_artist_id": 999}]})
        assert resp.status_code == 200
        assert resp.json()["results"][0]["bio"] is None

    @pytest.mark.asyncio
    async def test_blank_profile_is_null_bio(self, make_app, mock_cache):
        mock_cache.get_artist_details_bulk.return_value = {
            7: ArtistDetails(artist_id=7, name="Y", profile="   ")
        }
        ctx, app = make_app(service=None)
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Y", "discogs_artist_id": 7}]})
        assert resp.status_code == 200
        assert resp.json()["results"][0]["bio"] is None

    @pytest.mark.asyncio
    async def test_tombstone_row_is_null_bio(self, make_app, mock_cache):
        # not_found tombstone rows carry name="" + defaults; must be guarded.
        mock_cache.get_artist_details_bulk.return_value = {
            8: ArtistDetails(artist_id=8, name="", not_found=True)
        }
        ctx, app = make_app(service=None)
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Z", "discogs_artist_id": 8}]})
        assert resp.status_code == 200
        assert resp.json()["results"][0]["bio"] is None

    @pytest.mark.asyncio
    async def test_bulk_details_fetched_once_for_batch(self, make_app, mock_cache):
        # Budget guard: one cache-only bulk read per batch, with all non-null ids.
        ctx, app = make_app(service=None)
        with ctx:
            resp = await _post(
                app,
                {
                    "artists": [
                        {"artist_name": "A", "discogs_artist_id": 1},
                        {"artist_name": "B", "discogs_artist_id": 2},
                        {"artist_name": "NameOnly"},
                    ]
                },
            )
        assert resp.status_code == 200
        mock_cache.get_artist_details_bulk.assert_awaited_once()
        (called_ids,) = mock_cache.get_artist_details_bulk.await_args.args
        assert sorted(called_ids) == [1, 2]


class TestErrorSurface:
    @pytest.mark.asyncio
    async def test_no_cache_pool_returns_503(self, make_app):
        ctx, app = make_app(cache=None)
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Stereolab"}]})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_cache_unavailable_midflight_returns_503(self, make_app, mock_cache):
        mock_cache.aggregate_artist_genre_style.side_effect = CacheUnavailableError("pool gone")
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Stereolab"}]})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_empty_batch_returns_422(self, make_app):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"artists": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_field_returns_422(self, make_app):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_over_cap_returns_413(self, make_app, mock_cache):
        ctx, app = make_app()
        oversized = {
            "artists": [{"artist_name": f"Artist {i}"} for i in range(_GENRES_INPUT_CAP + 1)]
        }
        with ctx:
            resp = await _post(app, oversized)
        assert resp.status_code == 413
        mock_cache.aggregate_artist_genre_style.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, make_app):
        ctx, app = make_app()
        with ctx:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    _ROUTE, content=b"not json {", headers={"Content-Type": "application/json"}
                )
        assert resp.status_code == 400


class TestAuth:
    """Bearer ``LML_API_KEY`` posture, like streaming-check / the sibling routes."""

    @staticmethod
    def _auth_settings() -> Settings:
        return Settings(
            discogs_token=None,
            database_url_discogs=None,
            sentry_dsn=None,
            posthog_api_key=None,
            enable_telemetry=False,
            library_db_path="test_library.db",
            lml_require_auth=True,
            lml_api_key="s3cret",
        )

    @pytest.mark.asyncio
    async def test_missing_header_returns_401(self, make_app):
        ctx, app = make_app(settings=self._auth_settings())
        with ctx:
            resp = await _post(app, {"artists": [{"artist_name": "Stereolab"}]})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_returns_403(self, make_app):
        ctx, app = make_app(settings=self._auth_settings())
        with ctx:
            resp = await _post(
                app,
                {"artists": [{"artist_name": "Stereolab"}]},
                headers={"Authorization": "Bearer nope"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_correct_token_passes(self, make_app, mock_cache):
        mock_cache.aggregate_artist_genre_style.return_value = ({"Rock": 1}, {})
        ctx, app = make_app(settings=self._auth_settings())
        with ctx:
            resp = await _post(
                app,
                {"artists": [{"artist_name": "Stereolab"}]},
                headers={"Authorization": "Bearer s3cret"},
            )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["source"] == "cache"


class TestInputCap:
    def test_cap_is_documented_value(self):
        # A fully-cache-miss batch fans out ~1 + FALLBACK_MAX_RELEASES Discogs
        # calls per artist; 25 keeps the worst case inside Railway's ceiling.
        assert _GENRES_INPUT_CAP == 25
