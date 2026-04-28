"""Integration tests for the Phase 1.5 external-cache fallback.

End-to-end coverage of the ``include_external_caches`` opt-in on
``POST /api/v1/lookup``: ordering across library -> discogs -> MB,
and that legacy callers see no behavior change.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def app_client_with_external_caches(library_db, test_settings):
    """app_client with mocked discogs-cache and musicbrainz-cache PgSources."""
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_musicbrainz_pg,
        get_posthog_client,
    )
    from main import app

    discogs_cache = AsyncMock()
    discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
    discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
    discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])

    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(return_value=[])

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_discogs_cache_service] = lambda: discogs_cache
    app.dependency_overrides[get_musicbrainz_pg] = lambda: mb_pg
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Expose the mocks on the client for assertions / per-test programming
        client.discogs_cache = discogs_cache  # type: ignore[attr-defined]
        client.mb_pg = mb_pg  # type: ignore[attr-defined]
        yield client

    app.dependency_overrides.clear()


class TestExternalCacheFallback:
    @pytest.mark.asyncio
    async def test_legacy_caller_does_not_trigger_external_query(
        self, app_client_with_external_caches
    ):
        """Default request (no include_external_caches) preserves prior behavior."""
        client = app_client_with_external_caches
        resp = await client.post(
            "/api/v1/lookup",
            json={"artist": "ZZZNONEXISTENT", "raw_message": "ZZZNONEXISTENT"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        # external_source either omitted or null — both signal "no fallback ran"
        assert body.get("external_source") is None
        client.discogs_cache.search_artists_by_name.assert_not_called()
        client.mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_library_hit_marks_source_library(self, app_client_with_external_caches):
        """When the library has results, external caches are NOT consulted even with the flag."""
        client = app_client_with_external_caches
        resp = await client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "raw_message": "Stereolab",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert body["external_source"] == "library"
        client.discogs_cache.search_artists_by_name.assert_not_called()
        client.mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_discogs_fallback_returns_canonical_name(self, app_client_with_external_caches):
        """Library miss + discogs hit -> result with canonical artist + external_source='discogs'."""
        client = app_client_with_external_caches
        client.discogs_cache.search_artists_by_name.return_value = [
            {"id": 99, "name": "Astrid Øster Mortensen", "score": 0.71},
        ]

        resp = await client.post(
            "/api/v1/lookup",
            json={
                "artist": "Astrid ster Mortenson",
                "raw_message": "Astrid ster Mortenson",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_source"] == "discogs"
        assert len(body["results"]) == 1
        assert body["results"][0]["library_item"]["artist"] == "Astrid Øster Mortensen"
        client.mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_musicbrainz_fallback_when_discogs_misses(self, app_client_with_external_caches):
        """Library miss + discogs miss + MB hit -> external_source='musicbrainz'."""
        client = app_client_with_external_caches
        client.discogs_cache.search_artists_by_name.return_value = []
        client.mb_pg.fetchall.return_value = [
            {"id": "mb-uuid-1", "name": "Csillagrablók", "score": 0.83},
        ]

        resp = await client.post(
            "/api/v1/lookup",
            json={
                "artist": "Csillagrablok",
                "raw_message": "Csillagrablok",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_source"] == "musicbrainz"
        assert len(body["results"]) == 1
        assert body["results"][0]["library_item"]["artist"] == "Csillagrablók"

    @pytest.mark.asyncio
    async def test_album_skeleton_falls_back_to_discogs_releases(
        self, app_client_with_external_caches
    ):
        """Phase 1.7: a RELEASE_TITLE skeleton (album, no artist) hits the release leg."""
        client = app_client_with_external_caches
        client.discogs_cache.search_releases_by_title.return_value = [
            {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
        ]

        resp = await client.post(
            "/api/v1/lookup",
            json={
                "album": "ZZZNotInLibraryAlbum",
                "raw_message": "ZZZNotInLibraryAlbum",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_source"] == "discogs"
        assert len(body["results"]) == 1
        item = body["results"][0]["library_item"]
        assert item["artist"] == "Juana Molina"
        assert item["title"] == "DOGA"
        # Artist branch must NOT have fired.
        client.discogs_cache.search_artists_by_name.assert_not_called()
        client.discogs_cache.search_tracks_by_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_song_skeleton_falls_back_to_mb_recording(self, app_client_with_external_caches):
        """Phase 1.7: a SONG_TITLE skeleton with discogs miss + MB recording hit."""
        client = app_client_with_external_caches
        client.discogs_cache.search_tracks_by_title.return_value = []
        client.mb_pg.fetchall.return_value = [
            {"id": 42, "title": "la paradoja", "artist": "Juana Molina", "score": 0.85},
        ]

        resp = await client.post(
            "/api/v1/lookup",
            json={
                "song": "la paradja",
                "raw_message": "la paradja",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_source"] == "musicbrainz"
        assert len(body["results"]) == 1
        item = body["results"][0]["library_item"]
        assert item["artist"] == "Juana Molina"
        assert item["title"] == "la paradoja"
        # MB query SQL must reference mb_recording, not mb_release / mb_artist.
        sql = client.mb_pg.fetchall.call_args.args[0]
        assert "mb_recording" in sql

    @pytest.mark.asyncio
    async def test_label_only_request_skips_fallback(self, app_client_with_external_caches):
        """A bare raw_message (no typed field, e.g. LABEL_NAME case) skips the fallback."""
        client = app_client_with_external_caches

        resp = await client.post(
            "/api/v1/lookup",
            json={
                "raw_message": "ESP Disk'",
                "include_external_caches": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("external_source") is None
        client.discogs_cache.search_artists_by_name.assert_not_called()
        client.discogs_cache.search_releases_by_title.assert_not_called()
        client.discogs_cache.search_tracks_by_title.assert_not_called()
        client.mb_pg.fetchall.assert_not_called()
