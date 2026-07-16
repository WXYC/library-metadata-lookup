"""Tests for release/discogs_resolver.py — projects DiscogsService.get_release()
output to CanonicalRelease + ReleaseIdentifiers, with warnings on failure."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from generated.api_models import (
    DiscogsLabelCredit,
    DiscogsReleaseMetadata,
)
from release.discogs_resolver import (
    resolve_discogs_master,
    resolve_discogs_release,
)
from release.models import ResolveResult


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
        assert isinstance(result, ResolveResult)
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
    async def test_breaker_shed_returns_retriable_warning_without_error_log(
        self, mock_service, caplog
    ):
        """LML#814: ``get_release`` re-raises a saturation-breaker shed (LML#755
        FIX-1). The resolver must treat that shed as expected degrade rather than
        route it through the generic ``except`` that logs at ERROR via
        ``logger.exception`` — under the Sentry LoggingIntegration
        (event_level=ERROR) each such record becomes a discrete event,
        reproducing the #755 flood on the release-resolve path.

        The shed must: emit no ERROR-level record; return ``canonical=None`` (so
        the orchestrator's ``canonical is not None``-gated identity write-back is
        skipped — nothing negative is persisted); and carry a *transient*,
        retriable warning rather than the hard "lookup failed" phrasing.
        """
        from discogs.breaker import DiscogsBreakerOpenError

        mock_service.get_release.side_effect = DiscogsBreakerOpenError(
            "Discogs saturation breaker open"
        )

        with caplog.at_level(logging.DEBUG):
            result = await resolve_discogs_release(mock_service, "12345")

        assert result.canonical is None
        assert result.identifiers.discogs_release_id == 12345
        # A transient/retriable warning, not the hard "lookup failed" phrasing.
        assert len(result.warnings) == 1
        warning = result.warnings[0].lower()
        assert "12345" in result.warnings[0]
        assert any(hint in warning for hint in ("rate-limit", "temporarily", "try again"))
        # The heart of #814: no ERROR-level records (each would be a Sentry event
        # under event_level=ERROR).
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], f"breaker shed emitted ERROR record(s): {error_records}"

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_id(self, mock_service):
        result = await resolve_discogs_release(mock_service, "not-a-number")

        assert result.canonical is None
        assert result.identifiers.discogs_release_id is None
        assert len(result.warnings) == 1
        mock_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["0", "-1", "-12345"])
    async def test_rejects_non_positive_id_without_calling_service(self, mock_service, bad_id):
        """LML#546: Discogs release ids start at 1. A `release_id <= 0` value
        coming from user input or upstream synthesis must short-circuit before
        ``service.get_release`` is touched, because the 404 the service would
        get from Discogs writes a permanent tombstone (LML#510) that poisons
        the row for everyone. The resolver is the boundary; reject here.
        """
        result = await resolve_discogs_release(mock_service, bad_id)

        assert result.canonical is None
        assert len(result.warnings) == 1
        assert "positive" in result.warnings[0].lower()
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
