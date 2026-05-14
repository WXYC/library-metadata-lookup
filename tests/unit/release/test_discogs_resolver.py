"""Tests for release/discogs_resolver.py — projects DiscogsService.get_release()
output to CanonicalRelease + ReleaseIdentifiers, with warnings on failure."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from generated.api_models import (
    DiscogsLabelCredit,
    DiscogsReleaseMetadata,
)
from release.discogs_resolver import (
    DiscogsResolveResult,
    resolve_discogs_master,
    resolve_discogs_release,
)


def _make_release(**overrides) -> DiscogsReleaseMetadata:
    """Produce a minimal valid release for projection tests.

    ``cached`` defaults to True (the dominant production path); pass
    ``cached=False`` to simulate a live-API miss.
    """
    base = {
        "release_id": 12345,
        "title": "DOGA",
        "artist": "Juana Molina",
        "year": 2024,
        "label": "Sonamos",
        "artist_id": 999,
        "labels": [DiscogsLabelCredit(label_id=42, name="Sonamos", catno="SON-001")],
        "release_url": "https://www.discogs.com/release/12345",
        "cached": True,
    }
    base.update(overrides)
    return DiscogsReleaseMetadata.model_validate(base)


@pytest.fixture
def mock_service():
    return AsyncMock()


class TestResolveDiscogsRelease:
    @pytest.mark.asyncio
    async def test_projects_basic_release(self, mock_service):
        mock_service.get_release.return_value = _make_release()

        result = await resolve_discogs_release(mock_service, "12345")

        mock_service.get_release.assert_awaited_once_with(12345)
        assert isinstance(result, DiscogsResolveResult)
        assert result.canonical is not None
        assert result.canonical.artist == "Juana Molina"
        assert result.canonical.title == "DOGA"
        assert result.canonical.label == "Sonamos"
        assert result.canonical.catno == "SON-001"
        assert result.canonical.year == 2024
        assert result.identifiers.discogs_release_id == 12345
        assert result.identifiers.discogs_artist_id == 999
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_label_null_when_no_labels(self, mock_service):
        # A self-released album on Discogs sometimes has no labels at all.
        mock_service.get_release.return_value = _make_release(label=None, labels=[])

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.canonical is not None
        assert result.canonical.label is None
        assert result.canonical.catno is None

    @pytest.mark.asyncio
    async def test_catno_null_when_label_has_no_catno(self, mock_service):
        mock_service.get_release.return_value = _make_release(
            labels=[DiscogsLabelCredit(label_id=42, name="Sonamos", catno=None)]
        )

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.canonical is not None
        assert result.canonical.label == "Sonamos"
        assert result.canonical.catno is None

    @pytest.mark.asyncio
    async def test_returns_none_canonical_with_warning_on_miss(self, mock_service):
        # get_release returns None on rate-limit / not-found / API error.
        mock_service.get_release.return_value = None

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.canonical is None
        assert result.identifiers.discogs_release_id == 12345
        assert len(result.warnings) == 1
        assert "12345" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_returns_none_canonical_with_warning_on_exception(self, mock_service):
        mock_service.get_release.side_effect = RuntimeError("network exploded")

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.canonical is None
        assert result.identifiers.discogs_release_id == 12345
        assert any("failed" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_id(self, mock_service):
        result = await resolve_discogs_release(mock_service, "not-a-number")

        assert result.canonical is None
        assert result.identifiers.discogs_release_id is None
        assert len(result.warnings) == 1
        mock_service.get_release.assert_not_called()


class TestResolveDiscogsMaster:
    @pytest.mark.asyncio
    async def test_master_returns_warning_with_master_id(self, mock_service):
        # Master URLs aren't supported in v1; return a clear warning.
        result = await resolve_discogs_master(mock_service, "789")

        assert result.canonical is None
        assert result.identifiers.discogs_master_id == 789
        assert len(result.warnings) == 1
        assert "master" in result.warnings[0].lower()
        mock_service.get_release.assert_not_called()


class TestMatchedViaProvenance:
    """Per #329: the resolver must surface a `matched_via` tier so consumers
    can tell whether the release came from the local cache or the live
    Discogs API. The bool ``ReleaseMetadataResponse.cached`` is the
    authoritative signal — cache_service.get_release() sets cached=True,
    DiscogsService.get_release()'s API branch sets cached=False.
    """

    @pytest.mark.asyncio
    async def test_cache_hit_reports_discogs_cache(self, mock_service):
        mock_service.get_release.return_value = _make_release(cached=True)

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.matched_via == "discogs_cache"

    @pytest.mark.asyncio
    async def test_live_api_hit_reports_discogs_live_api(self, mock_service):
        mock_service.get_release.return_value = _make_release(cached=False)

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.matched_via == "discogs_live_api"

    @pytest.mark.asyncio
    async def test_miss_reports_none(self, mock_service):
        # Cache miss + API failure: matched_via stays None.
        mock_service.get_release.return_value = None

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.matched_via is None

    @pytest.mark.asyncio
    async def test_exception_reports_none(self, mock_service):
        mock_service.get_release.side_effect = RuntimeError("network exploded")

        result = await resolve_discogs_release(mock_service, "12345")

        assert result.matched_via is None

    @pytest.mark.asyncio
    async def test_non_numeric_id_reports_none(self, mock_service):
        result = await resolve_discogs_release(mock_service, "not-a-number")

        assert result.matched_via is None

    @pytest.mark.asyncio
    async def test_live_api_hit_records_metric(self, mock_service):
        """The live-tier metric is the only way to see freshness gap impact."""
        mock_service.get_release.return_value = _make_release(cached=False)

        with patch("release.discogs_resolver.get_cache_stats_recorder") as recorder_factory:
            recorder = recorder_factory.return_value
            await resolve_discogs_release(mock_service, "12345")

        recorder.record.assert_called_once_with("discogs_live_release_hit")

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_record_live_metric(self, mock_service):
        mock_service.get_release.return_value = _make_release(cached=True)

        with patch("release.discogs_resolver.get_cache_stats_recorder") as recorder_factory:
            recorder = recorder_factory.return_value
            await resolve_discogs_release(mock_service, "12345")

        recorder.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_miss_does_not_record_live_metric(self, mock_service):
        mock_service.get_release.return_value = None

        with patch("release.discogs_resolver.get_cache_stats_recorder") as recorder_factory:
            recorder = recorder_factory.return_value
            await resolve_discogs_release(mock_service, "12345")

        recorder.record.assert_not_called()
