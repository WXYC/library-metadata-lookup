"""Tombstone-on-404 behavior for `DiscogsService.get_release` / `get_artist_details` (LML#510).

The `_api_fetch` inner functions previously collapsed 404 to `None`; the
fallthrough seam refused to write `None` to PG, so every caller re-asked
Discogs on the next call for the same id and re-burned the rate-limit
budget. This test module pins the new behavior:

* `_api_fetch` discriminates 404 from other failures and returns a
  tombstone-shaped model (`not_found = True`, identifier columns set to
  empty-string sentinels).
* The fallthrough seam writes the tombstone via the existing `pg_write`.
* The public `DiscogsService.get_release` / `get_artist_details` translate
  the tombstone back to `None` so callers' contract (`T | None`) is
  preserved.
* `CachedOnlyResolver` (in `discogs.markup_parser`) reads `cache_service`
  directly and must also guard against the tombstone sentinel; otherwise
  it renders an empty-string display name.
* Telemetry: `record("release_404_tombstone_written")` /
  `record("artist_404_tombstone_written")` on the write side;
  `record("tombstone_returned")` on the read side;
  `record("release_5xx_passthrough")` for non-tombstone Discogs outages
  so the metric stays interpretable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discogs.markup_parser import CachedOnlyResolver
from discogs.models import ArtistDetails, ReleaseMetadataResponse
from discogs.service import DiscogsService


@pytest.fixture
def service():
    return DiscogsService(token="test-token")


@pytest.fixture
def service_with_cache():
    cache_svc = AsyncMock()
    return DiscogsService(token="test-token", cache_service=cache_svc)


def _mock_response(status: int) -> MagicMock:
    """Build a MagicMock httpx-Response-shaped object."""
    resp = MagicMock()
    resp.status_code = status
    # raise_for_status mirrors httpx semantics: only 4xx / 5xx raise.
    if status >= 400:

        def _raise():
            import httpx

            req = MagicMock()
            raise httpx.HTTPStatusError(
                f"{status}", request=req, response=MagicMock(status_code=status)
            )

        resp.raise_for_status = _raise
    else:
        resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# _api_fetch 404 discrimination
# ---------------------------------------------------------------------------


class TestReleaseApiFetch404:
    """The release `_api_fetch` inner function on a Discogs 404 response."""

    @pytest.mark.asyncio
    async def test_404_tombstone_no_cache_returns_none(self, service):
        """Without a cache_service the public method returns `_api_fetch`'s
        value directly. The boundary translation must run there too so
        callers always see `None` for a tombstoned id."""
        resp = _mock_response(404)
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            result = await service.get_release(999_999_999)
        assert result is None, (
            "Public `get_release` must return None on a 404 even when "
            "cache_service is None (the no-cache early-return path also "
            "needs the tombstone→None translation)."
        )

    @pytest.mark.asyncio
    async def test_404_records_write_counter(self, service):
        """`record('release_404_tombstone_written')` fires at the
        discrimination point so we can observe tombstone-write volume in
        cache_stats."""
        recorder = MagicMock()
        resp = _mock_response(404)
        with (
            patch.object(service, "_request_with_retry", new_callable=AsyncMock, return_value=resp),
            patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
        ):
            await service.get_release(999_999_999)
        recorder.record.assert_any_call("release_404_tombstone_written")

    @pytest.mark.asyncio
    async def test_5xx_records_passthrough_counter(self, service):
        """5xx is a transient outage, not a tombstone. The passthrough
        counter keeps Discogs-outage signal legible separately from
        tombstone activity."""
        recorder = MagicMock()
        resp = _mock_response(503)
        with (
            patch.object(service, "_request_with_retry", new_callable=AsyncMock, return_value=resp),
            patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
        ):
            result = await service.get_release(123)
        assert result is None, "5xx must not produce a tombstone."
        recorder.record.assert_any_call("release_5xx_passthrough")

    @pytest.mark.asyncio
    async def test_5xx_does_not_record_tombstone_counter(self, service):
        """Critical: a 5xx must not poison the cache with a false tombstone."""
        recorder = MagicMock()
        resp = _mock_response(500)
        with (
            patch.object(service, "_request_with_retry", new_callable=AsyncMock, return_value=resp),
            patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
        ):
            await service.get_release(123)
        for call in recorder.record.call_args_list:
            assert call.args[0] != "release_404_tombstone_written", (
                "5xx must not record release_404_tombstone_written — the "
                "tombstone counter is reserved for actual 404s."
            )

    @pytest.mark.asyncio
    async def test_request_error_returns_none_no_tombstone(self, service):
        """When `_request_with_retry` returns None (network failure, rate
        limit exhaustion), no tombstone is written and no counter fires."""
        recorder = MagicMock()
        with (
            patch.object(service, "_request_with_retry", new_callable=AsyncMock, return_value=None),
            patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
        ):
            result = await service.get_release(123)
        assert result is None
        for call in recorder.record.call_args_list:
            assert call.args[0] != "release_404_tombstone_written"


class TestArtistApiFetch404:
    """Parallel tests for `get_artist_details`."""

    @pytest.mark.asyncio
    async def test_404_no_cache_returns_none(self, service):
        resp = _mock_response(404)
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            result = await service.get_artist_details(999_999_999)
        assert result is None, (
            "Public `get_artist_details` must return None on 404, even on "
            "the no-cache early-return path."
        )

    @pytest.mark.asyncio
    async def test_404_records_write_counter(self, service):
        recorder = MagicMock()
        resp = _mock_response(404)
        with (
            patch.object(service, "_request_with_retry", new_callable=AsyncMock, return_value=resp),
            patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
        ):
            await service.get_artist_details(999_999_999)
        recorder.record.assert_any_call("artist_404_tombstone_written")

    @pytest.mark.asyncio
    async def test_log_level_error_not_warning(self, service, caplog):
        """The artist bare-except path was previously `logger.warning`,
        masking artist 404s from Sentry's default `error+` filter. Per the
        issue's observability gap finding, the artist log must be promoted
        to `logger.error` so the tombstone write counter has a Sentry
        corroboration surface."""
        # Simulate the unhandled-error path (not the 404 path, which now
        # handles 404 without raising). A non-404 4xx is the easiest probe.
        resp = _mock_response(418)
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            import logging

            with caplog.at_level(logging.WARNING, logger="discogs.service"):
                await service.get_artist_details(123)
        error_messages = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("artist" in r.message.lower() for r in error_messages), (
            "Artist failure path must log at ERROR level (was WARNING per "
            "Sentry observability gap finding in LML#510)."
        )


# ---------------------------------------------------------------------------
# Boundary translation: public method translates `not_found` → None
# ---------------------------------------------------------------------------


class TestReleasePublicBoundary:
    """The public `DiscogsService.get_release` translates tombstones back
    to `None` regardless of which leg (PG or API) produced them."""

    @pytest.mark.asyncio
    async def test_pg_tombstone_returns_none(self, service_with_cache):
        """When PG returns a tombstone-shaped row, the public method must
        translate to None so callers' `T | None` contract is preserved."""
        tombstone = ReleaseMetadataResponse(
            release_id=42,
            title="",
            artist="",
            release_url="https://www.discogs.com/release/42",
            not_found=True,
            artwork_checked_at=datetime.now(UTC),
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=tombstone)
        service_with_cache.cache_service.write_release = AsyncMock()

        # API leg should never run because the PG hit is_pg_hit=True (the
        # tombstone has artwork_checked_at set).
        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.get_release(42)

        assert result is None
        mock_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_pg_tombstone_records_returned_counter(self, service_with_cache):
        """Read-side telemetry: `record('tombstone_returned')` fires when
        a tombstone is observed at the public boundary so the
        `pg_cache_hit - tombstone_returned` dashboard math stays correct."""
        tombstone = ReleaseMetadataResponse(
            release_id=42,
            title="",
            artist="",
            release_url="https://www.discogs.com/release/42",
            not_found=True,
            artwork_checked_at=datetime.now(UTC),
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=tombstone)
        service_with_cache.cache_service.write_release = AsyncMock()

        recorder = MagicMock()
        with patch("discogs.service.get_cache_stats_recorder", return_value=recorder):
            await service_with_cache.get_release(42)
        recorder.record.assert_any_call("tombstone_returned")


class TestArtistPublicBoundary:
    @pytest.mark.asyncio
    async def test_pg_tombstone_returns_none(self, service_with_cache):
        tombstone = ArtistDetails(
            artist_id=42,
            name="",
            not_found=True,
            fetched_at=datetime.now(UTC),
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=tombstone)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.get_artist_details(42)

        assert result is None
        mock_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_pg_tombstone_records_returned_counter(self, service_with_cache):
        tombstone = ArtistDetails(
            artist_id=42,
            name="",
            not_found=True,
            fetched_at=datetime.now(UTC),
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=tombstone)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        recorder = MagicMock()
        with patch("discogs.service.get_cache_stats_recorder", return_value=recorder):
            await service_with_cache.get_artist_details(42)
        recorder.record.assert_any_call("tombstone_returned")


# ---------------------------------------------------------------------------
# CachedOnlyResolver boundary: read cache_service directly, guard on tombstone
# ---------------------------------------------------------------------------


class TestCachedOnlyResolverGuards:
    """`CachedOnlyResolver` bypasses `DiscogsService` and reads
    `cache_service` directly. After the cache-service short-circuit
    returns the tombstone shape with `name = ""` / `title = ""`,
    `resolve_artist` / `resolve_release` would return `""` instead of
    `None`, causing the markup parser to render an `artistLink` token
    with `display_name=""`. The resolver must guard."""

    @pytest.mark.asyncio
    async def test_resolve_artist_returns_none_for_tombstone(self):
        cache = AsyncMock()
        cache.get_artist_details = AsyncMock(
            return_value=ArtistDetails(
                artist_id=42,
                name="",
                not_found=True,
                fetched_at=datetime.now(UTC),
            )
        )
        resolver = CachedOnlyResolver(cache)
        result = await resolver.resolve_artist(42)
        assert result is None, (
            "Tombstone with name='' must translate to None at this layer; "
            "otherwise the markup parser emits a token with display_name=''."
        )

    @pytest.mark.asyncio
    async def test_resolve_release_returns_none_for_tombstone(self):
        cache = AsyncMock()
        cache.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=42,
                title="",
                artist="",
                release_url="https://www.discogs.com/release/42",
                not_found=True,
                artwork_checked_at=datetime.now(UTC),
            )
        )
        resolver = CachedOnlyResolver(cache)
        result = await resolver.resolve_release(42)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_artist_returns_name_for_real_row(self):
        """Counter-test: a real artist row still resolves to its name."""
        cache = AsyncMock()
        cache.get_artist_details = AsyncMock(
            return_value=ArtistDetails(
                artist_id=42,
                name="Stereolab",
                not_found=False,
                fetched_at=datetime.now(UTC),
            )
        )
        resolver = CachedOnlyResolver(cache)
        result = await resolver.resolve_artist(42)
        assert result == "Stereolab"
