"""Tests for release/discogs_resolver.py — projects DiscogsService.get_release()
output to CanonicalRelease + ReleaseIdentifiers, with warnings on failure."""

from __future__ import annotations

from unittest.mock import AsyncMock

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
    """Produce a minimal valid release for projection tests."""
    base = {
        "release_id": 12345,
        "title": "DOGA",
        "artist": "Juana Molina",
        "year": 2024,
        "label": "Sonamos",
        "artist_id": 999,
        "labels": [DiscogsLabelCredit(label_id=42, name="Sonamos", catno="SON-001")],
        "release_url": "https://www.discogs.com/release/12345",
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
