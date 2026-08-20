"""Emit-site coverage for the LML#1233 miss-attribution properties.

`tests/unit/test_miss_kind.py` covers the taxonomy in isolation; this module
covers the wiring -- that the seven new properties actually reach the
`lookup_completed` payload, carrying values read off the real response rather
than defaults.

That split matters because the failure mode being guarded is silent. If
`miss_kind` never reaches PostHog, or reaches it hardcoded, the Layer 2 alert
does not error -- it reports a healthy miss rate forever. So these tests
assert on the captured payload, not on the derivation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from generated.api_models import DegradedReason
from library.db import LibraryDB
from lookup.miss_kind import MISS_CLEAN, MISS_DEGRADED, MISS_TIMEOUT, OUTCOME_HIT
from lookup.models import LookupResponse, LookupResultItem
from tests.factories import LOOKUP_BODY, make_catalog_item, make_match_result
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_db():
    return AsyncMock(spec=LibraryDB)


@pytest.fixture
def mock_discogs():
    return AsyncMock(spec=DiscogsService)


def _a_result_item() -> LookupResultItem:
    """One well-formed result row, so the response reads as a hit."""
    return LookupResultItem(
        library_item=make_catalog_item(id=10, artist="Stereolab", title="Aluminum Tunes"),
        artwork=make_match_result(release_id=12345),
    )


def _completed_props(mock_posthog: Mock) -> dict[str, Any]:
    """Pull the `lookup_completed` payload out of the capture calls.

    The handler emits exactly one summary event (per-step events are
    suppressed on `/lookup`), but filtering by name rather than taking the
    last call keeps this from silently reading the wrong event if that ever
    changes.
    """
    for call in mock_posthog.capture.call_args_list:
        if call.kwargs.get("event") == "lookup_completed":
            return call.kwargs["properties"]
    raise AssertionError(
        f"no lookup_completed event captured; saw "
        f"{[c.kwargs.get('event') for c in mock_posthog.capture.call_args_list]}"
    )


async def _capture_props(mock_db, mock_discogs, mock_settings, response) -> dict[str, Any]:
    """Drive one `/lookup` request and return its summary properties."""
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    mock_posthog = Mock()
    mock_posthog.capture = Mock()
    mock_posthog.flush = Mock()

    with override_deps(
        app,
        {
            get_library_db: mock_db,
            get_discogs_service: mock_discogs,
            get_posthog_client: mock_posthog,
            get_settings: mock_settings,
        },
    ):
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)
        assert resp.status_code == 200

    return _completed_props(mock_posthog)


class TestMissKindReachesPostHog:
    """The `miss_kind` value must be derived from the actual response."""

    @pytest.mark.asyncio
    async def test_clean_miss(self, mock_db, mock_discogs, mock_settings) -> None:
        props = await _capture_props(
            mock_db, mock_discogs, mock_settings, LookupResponse(results=[], search_type="direct")
        )
        assert props["miss_kind"] == MISS_CLEAN

    @pytest.mark.asyncio
    async def test_timeout_miss(self, mock_db, mock_discogs, mock_settings) -> None:
        """A hard-cap trip must not be counted as a clean miss.

        This is the case that would otherwise make an infrastructure incident
        look like a recall regression.
        """
        props = await _capture_props(
            mock_db,
            mock_discogs,
            mock_settings,
            LookupResponse(results=[], search_type="none", timeout=True, song_not_found=True),
        )
        assert props["miss_kind"] == MISS_TIMEOUT

    @pytest.mark.asyncio
    async def test_degraded_miss(self, mock_db, mock_discogs, mock_settings) -> None:
        props = await _capture_props(
            mock_db,
            mock_discogs,
            mock_settings,
            LookupResponse(
                results=[],
                search_type="direct",
                degraded=True,
                degraded_reason=DegradedReason.upstream_unavailable,
            ),
        )
        assert props["miss_kind"] == MISS_DEGRADED
        assert props["degraded"] is True
        assert props["degraded_reason"] == DegradedReason.upstream_unavailable.value

    @pytest.mark.asyncio
    async def test_hit_is_not_a_miss(self, mock_db, mock_discogs, mock_settings) -> None:
        props = await _capture_props(
            mock_db,
            mock_discogs,
            mock_settings,
            LookupResponse(results=[_a_result_item()], search_type="direct"),
        )
        assert props["miss_kind"] == OUTCOME_HIT
        assert props["results_count"] == 1


class TestOutcomeFieldsReachPostHog:
    """The five response fields ride alongside for the runbook's splits."""

    @pytest.mark.asyncio
    async def test_all_outcome_fields_present(self, mock_db, mock_discogs, mock_settings) -> None:
        props = await _capture_props(
            mock_db,
            mock_discogs,
            mock_settings,
            LookupResponse(results=[], search_type="fallback", song_not_found=True),
        )
        for key in (
            "miss_kind",
            "timeout",
            "degraded",
            "degraded_reason",
            "song_not_found",
            "found_on_compilation",
        ):
            assert key in props, f"{key} missing from lookup_completed"
        assert props["song_not_found"] is True
        assert props["timeout"] is False
        assert props["degraded_reason"] is None

    @pytest.mark.asyncio
    async def test_degraded_reason_is_wire_value_not_enum(
        self, mock_db, mock_discogs, mock_settings
    ) -> None:
        """PostHog must receive the string, not a repr of the enum.

        `DegradedReason` is a `StrEnum`, so a bare pass-through happens to
        serialize correctly today -- but an insight filtering on
        `'cache_only'` breaks the moment that stops being true, and it breaks
        silently. Pin the wire form.
        """
        props = await _capture_props(
            mock_db,
            mock_discogs,
            mock_settings,
            LookupResponse(
                results=[],
                search_type="direct",
                degraded=True,
                degraded_reason=DegradedReason.cache_only,
            ),
        )
        assert props["degraded_reason"] == "cache_only"
        assert isinstance(props["degraded_reason"], str)


class TestCommitSha:
    """Deploy attribution -- the property that makes a breakdown possible."""

    @pytest.mark.asyncio
    async def test_commit_sha_key_always_present(
        self, mock_db, mock_discogs, mock_settings
    ) -> None:
        """Present even when unresolved.

        Locally and in CI there is no baked file and no Railway env var, so
        the value is `None`. The key must still be emitted: an absent
        property and a null one are the same to a PostHog filter, but a key
        that appears only on some deploys makes the breakdown unreadable.
        """
        props = await _capture_props(
            mock_db, mock_discogs, mock_settings, LookupResponse(results=[], search_type="direct")
        )
        assert "commit_sha" in props

    @pytest.mark.asyncio
    async def test_commit_sha_carries_resolved_value(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ) -> None:
        """The emitted value is the one the module resolved at import.

        Patching the module-level binding (rather than the resolver) is
        deliberate: it asserts the hot path reads a pre-bound constant, which
        is the property that keeps a per-request file read off `/lookup`.
        """
        import lookup.router as router_mod

        monkeypatch.setattr(router_mod, "COMMIT_SHA", "cafe123", raising=True)
        props = await _capture_props(
            mock_db, mock_discogs, mock_settings, LookupResponse(results=[], search_type="direct")
        )
        assert props["commit_sha"] == "cafe123"
