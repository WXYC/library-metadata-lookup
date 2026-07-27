"""LML#944: traffic-class observability lead set — endpoint-family.

Carved out of the #931 umbrella (traffic-class-aware observability) as the
**drain-independent** lead set: this dimension needs no new lanes, so it lands
before the controlled prod bulk drain ([#929](https://github.com/WXYC/library-metadata-lookup/issues/929))
instead of waiting on it.

``lml.endpoint_family`` distinguishes ``/lookup`` from ``/lookup/bulk`` traffic
via a Sentry tag (``"lookup"`` | ``"lookup_bulk"``) plus a matching PostHog
``endpoint_family`` property, recorded once per request/batch context right
after ``_record_event_loop_lag`` — the same call-site pattern as the #681
``_record_lml_flag_tags``. Unlike those, this dimension is string-valued, so it
cannot ride the numeric-only ``cache_stats`` seam (``_LML_CACHE_STATS_EXTRA_KEYS``,
``dict[str, float]`` via ``CacheStatsRecorder.record()``); it uses the sibling
per-request Sentry-tag / PostHog-freeform-dict channel instead.

The companion ``lml.caller_budget_ms`` -> ``set_measurement`` promotion lives in
``tests/unit/test_search.py::TestCallerBudget`` alongside the existing
``set_data`` coverage it extends.

Forward-compatible: LML#929 is expected to add ``"library_search"`` as a third
``endpoint_family`` value; no rework needed here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from lookup.endpoint_family import (
    ENDPOINT_FAMILY_LOOKUP,
    ENDPOINT_FAMILY_LOOKUP_BULK,
    record_endpoint_family_tag,
)
from lookup.models import LookupResponse
from main import app
from tests.factories import LOOKUP_BODY
from tests.unit.conftest import override_deps


class TestRecordEndpointFamilyTag:
    """Direct unit coverage for the LML#944 Sentry-tag helper.

    Mirrors ``_record_lml_flag_tags``'s shape: a plain ``sentry_sdk.set_tag``
    call whose failure must never break the request path.
    """

    def test_records_tag_for_lookup(self):
        set_tag = Mock()
        with patch("lookup.endpoint_family.sentry_sdk.set_tag", set_tag):
            record_endpoint_family_tag(ENDPOINT_FAMILY_LOOKUP)

        set_tag.assert_called_once_with("lml.endpoint_family", "lookup")

    def test_records_tag_for_lookup_bulk(self):
        set_tag = Mock()
        with patch("lookup.endpoint_family.sentry_sdk.set_tag", set_tag):
            record_endpoint_family_tag(ENDPOINT_FAMILY_LOOKUP_BULK)

        set_tag.assert_called_once_with("lml.endpoint_family", "lookup_bulk")

    def test_swallows_sdk_errors(self):
        """Any SDK-side exception is swallowed so observability cannot break /lookup."""
        with patch(
            "lookup.endpoint_family.sentry_sdk.set_tag",
            side_effect=RuntimeError("sentry exploded"),
        ):
            record_endpoint_family_tag(ENDPOINT_FAMILY_LOOKUP)  # must not raise


def _completed_properties(mock_posthog: Mock) -> dict:
    """Pull the full properties dict off the PostHog ``*_completed`` event.

    ``endpoint_family`` (LML#944) is a top-level property, a sibling of
    ``cache``, not nested inside it — unlike the #681 flag tags, which ride
    the ``cache`` sub-dict via ``cache_stats``.
    """
    for call in mock_posthog.capture.call_args_list:
        event = call.kwargs.get("event", "")
        if event.endswith("_completed"):
            return call.kwargs["properties"]
    raise AssertionError("no *_completed PostHog event captured")


async def _post(
    endpoint: str,
    json_body: dict,
    *,
    mock_posthog: Mock,
    set_tag: Mock,
    mock_settings,
) -> None:
    """POST to a lookup endpoint with ``perform_lookup``, PostHog, and
    ``sentry_sdk.set_tag`` all mocked. Shared scaffolding for both endpoints,
    mirroring ``_post_for_cache_props`` in ``test_rowless_flag_observability.py``.
    """
    with (
        override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: AsyncMock(),
                get_posthog_client: mock_posthog,
                get_settings: mock_settings,
            },
        ),
        patch("lookup.endpoint_family.sentry_sdk.set_tag", set_tag),
        patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=LookupResponse(results=[], search_type="none"),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(endpoint, json=json_body)

    assert resp.status_code == 200


class TestEndpointFamilyWiredIntoHandlers:
    """End-to-end (via ASGI client): both handlers call the helper with the
    right family value, and PostHog's ``*_completed`` event carries the
    sibling ``endpoint_family`` property (LML#944)."""

    @pytest.mark.asyncio
    async def test_lookup_tags_and_posthog_property(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup",
            LOOKUP_BODY,
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
        )

        assert ("lml.endpoint_family", ENDPOINT_FAMILY_LOOKUP) in [
            c.args for c in set_tag.call_args_list
        ]
        assert _completed_properties(mock_posthog)["endpoint_family"] == ENDPOINT_FAMILY_LOOKUP

    @pytest.mark.asyncio
    async def test_bulk_lookup_tags_and_posthog_property(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup/bulk",
            {"items": [{"artist": "Stereolab", "album": "Aluminum Tunes"}]},
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
        )

        assert ("lml.endpoint_family", ENDPOINT_FAMILY_LOOKUP_BULK) in [
            c.args for c in set_tag.call_args_list
        ]
        assert _completed_properties(mock_posthog)["endpoint_family"] == ENDPOINT_FAMILY_LOOKUP_BULK
