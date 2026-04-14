"""Integration tests for cache DB fallback when release rows are corrupted.

Verifies that when the PostgreSQL cache returns corrupted or unexpected data
for release queries, the DiscogsCacheService raises CacheUnavailableError
and logs the degradation, allowing callers to fall back to the Discogs API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService

pytestmark = pytest.mark.integration


class TestCacheFallbackOnCorruptedData:
    """Cache DB with corrupted release rows triggers fallback to API."""

    @pytest.fixture
    def corrupt_pool(self):
        """AsyncMock pool that simulates various corrupted DB states."""
        pool = AsyncMock()
        return pool

    @pytest.fixture
    def cache_service(self, corrupt_pool):
        return DiscogsCacheService(corrupt_pool)

    @pytest.mark.asyncio
    async def test_search_releases_db_error_raises_unavailable(self, cache_service, corrupt_pool):
        """When the DB raises an exception during search, CacheUnavailableError is raised."""
        corrupt_pool.fetch = AsyncMock(
            side_effect=Exception("could not read block 42 in file: Input/output error")
        )

        with pytest.raises(CacheUnavailableError, match="Cache search_releases failed"):
            await cache_service.search_releases(artist="Autechre", album="Confield")

    @pytest.mark.asyncio
    async def test_search_releases_by_track_db_error_raises_unavailable(
        self, cache_service, corrupt_pool
    ):
        """Track search with corrupted DB raises CacheUnavailableError."""
        corrupt_pool.fetch = AsyncMock(
            side_effect=Exception("missing chunk number 0 for toast value")
        )

        with pytest.raises(CacheUnavailableError, match="Cache search failed"):
            await cache_service.search_releases_by_track("VI Scose Poise", "Autechre")

    @pytest.mark.asyncio
    async def test_get_release_corrupted_fetchrow_raises_unavailable(
        self, cache_service, corrupt_pool
    ):
        """When fetchrow raises on corrupted release row, CacheUnavailableError is raised."""
        corrupt_pool.fetchrow = AsyncMock(
            side_effect=Exception("invalid memory alloc request size")
        )

        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release(28138)

    @pytest.mark.asyncio
    async def test_get_release_corrupted_child_tables_raises_unavailable(
        self, cache_service, corrupt_pool
    ):
        """Corrupted child table data (tracks, artists) during get_release raises error."""
        corrupt_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": None,
                "released": "2001-04-30",
            }
        )
        # Child table queries fail with corruption
        corrupt_pool.fetch = AsyncMock(
            side_effect=Exception("could not read block in relation release_track")
        )

        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release(28138)

    @pytest.mark.asyncio
    async def test_validate_track_db_error_raises_unavailable(self, cache_service, corrupt_pool):
        """validate_track_on_release raises CacheUnavailableError on DB corruption."""
        corrupt_pool.fetchval = AsyncMock(side_effect=Exception("data file corruption detected"))

        with pytest.raises(CacheUnavailableError):
            await cache_service.validate_track_on_release(28138, "VI Scose Poise", "Autechre")

    @pytest.mark.asyncio
    async def test_autocomplete_tracks_db_error_raises_unavailable(
        self, cache_service, corrupt_pool
    ):
        """autocomplete_tracks raises CacheUnavailableError on DB corruption."""
        corrupt_pool.fetch = AsyncMock(side_effect=Exception("corrupted page pointer"))

        with pytest.raises(CacheUnavailableError, match="Cache autocomplete_tracks failed"):
            await cache_service.autocomplete_tracks(artist="Stereolab", q="Fus")

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_corruption(self, cache_service, corrupt_pool):
        """is_available returns False (not raises) when DB is corrupted."""
        corrupt_pool.fetchval = AsyncMock(
            side_effect=Exception("the database system is in recovery mode")
        )

        result = await cache_service.is_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_write_release_db_error_raises_unavailable(self, cache_service, corrupt_pool):
        """write_release raises CacheUnavailableError when DB write fails."""
        from unittest.mock import MagicMock

        from discogs.models import ReleaseMetadataResponse, TrackItem

        acq_ctx = MagicMock()
        acq_ctx.__aenter__ = AsyncMock(
            side_effect=Exception("could not extend file: No space left on device")
        )
        acq_ctx.__aexit__ = AsyncMock(return_value=False)
        corrupt_pool.acquire = MagicMock(return_value=acq_ctx)

        release = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            year=2001,
            release_url="https://discogs.com/release/28138",
            tracklist=[TrackItem(position="1", title="VI Scose Poise")],
        )

        with pytest.raises(CacheUnavailableError):
            await cache_service.write_release(release)


class TestCacheDegradationLogging:
    """Verify that cache degradation is logged at the appropriate level."""

    @pytest.mark.asyncio
    async def test_search_error_is_logged(self, caplog):
        """DB errors during search are logged as errors."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("connection reset by peer"))
        service = DiscogsCacheService(pool)

        with pytest.raises(CacheUnavailableError):
            await service.search_releases(artist="Juana Molina", album="DOGA")

        assert any(
            "Cache search" in record.message and "failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_health_check_failure_logged_as_warning(self, caplog):
        """is_available logs degradation at WARNING level."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(side_effect=Exception("timeout"))
        service = DiscogsCacheService(pool)

        result = await service.is_available()
        assert result is False
        assert any("Cache health check failed" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_get_release_error_is_logged(self, caplog):
        """DB errors during get_release are logged."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(side_effect=Exception("connection refused"))
        service = DiscogsCacheService(pool)

        with pytest.raises(CacheUnavailableError):
            await service.get_release(12345)

        assert any("Cache" in record.message for record in caplog.records)
