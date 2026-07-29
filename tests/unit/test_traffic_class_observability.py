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
from lookup.admission import ADMISSION_WOULD_SHED_STAT_KEY
from lookup.endpoint_family import (
    ENDPOINT_FAMILY_LOOKUP,
    ENDPOINT_FAMILY_LOOKUP_BULK,
    record_endpoint_family_tag,
    record_low_priority_tag,
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


class TestRecordLowPriorityTag:
    """Direct unit coverage for the LML#930 PR2 traffic-class Sentry-tag helper.

    Mirrors ``TestRecordEndpointFamilyTag``'s shape exactly.
    """

    def test_records_true(self):
        set_tag = Mock()
        with patch("lookup.endpoint_family.sentry_sdk.set_tag", set_tag):
            record_low_priority_tag(True)

        set_tag.assert_called_once_with("lml.low_priority", "true")

    def test_records_false(self):
        set_tag = Mock()
        with patch("lookup.endpoint_family.sentry_sdk.set_tag", set_tag):
            record_low_priority_tag(False)

        set_tag.assert_called_once_with("lml.low_priority", "false")

    def test_swallows_sdk_errors(self):
        """Any SDK-side exception is swallowed so observability cannot break /lookup."""
        with patch(
            "lookup.endpoint_family.sentry_sdk.set_tag",
            side_effect=RuntimeError("sentry exploded"),
        ):
            record_low_priority_tag(True)  # must not raise


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
    headers: dict | None = None,
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
            resp = await ac.post(endpoint, json=json_body, headers=headers or {})

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


class TestLowPriorityWiredIntoHandlers:
    """End-to-end (via ASGI client): LML#930 PR2's ``lml.low_priority`` Sentry
    tag + PostHog property on both handlers.

    ``handle_lookup`` resolves ``low_priority`` from ``X-Caller-Class``
    (class 5 -> true; classes 1-4, absent, and invalid values -> false, same
    down-rank-only invariant ``TestCallerClassLowPriorityLane`` in
    ``test_lookup_router.py`` pins for the bulk-permit routing). ``handle_bulk_lookup``
    tags/sets it ``true`` unconditionally -- every item on that route is
    always low priority, regardless of any ``X-Caller-Class`` value sent.
    """

    @pytest.mark.asyncio
    async def test_lookup_class_five_tags_and_posthog_property_true(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup",
            LOOKUP_BODY,
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
            headers={"X-Caller-Class": "5"},
        )

        assert ("lml.low_priority", "true") in [c.args for c in set_tag.call_args_list]
        assert _completed_properties(mock_posthog)["low_priority"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-Caller-Class": "1"},
            {"X-Caller-Class": "2"},
            {"X-Caller-Class": "3"},
            {"X-Caller-Class": "4"},
            {"X-Caller-Class": "0"},
            {"X-Caller-Class": "6"},
            {"X-Caller-Class": "not-a-class"},
        ],
        ids=[
            "absent",
            "class-1",
            "class-2",
            "class-3",
            "class-4",
            "out-of-range-low",
            "out-of-range-high",
            "non-numeric",
        ],
    )
    async def test_lookup_non_class_five_tags_and_posthog_property_false(
        self, mock_settings, headers
    ):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup",
            LOOKUP_BODY,
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
            headers=headers,
        )

        assert ("lml.low_priority", "false") in [c.args for c in set_tag.call_args_list]
        assert _completed_properties(mock_posthog)["low_priority"] is False

    @pytest.mark.asyncio
    async def test_bulk_lookup_tags_and_posthog_property_true_unconditionally(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup/bulk",
            {"items": [{"artist": "Stereolab", "album": "Aluminum Tunes"}]},
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
            # Even an "escalation" attempt (a low X-Caller-Class value) cannot
            # move a bulk caller off the low-priority lane -- down-rank only.
            headers={"X-Caller-Class": "1"},
        )

        assert ("lml.low_priority", "true") in [c.args for c in set_tag.call_args_list]
        assert _completed_properties(mock_posthog)["low_priority"] is True


class TestAdmissionWouldShedStatKeySeeded:
    """LML#930 PR2: ``admission_would_shed`` is seeded into
    ``_LML_CACHE_STATS_EXTRA_KEYS`` so the would-shed rate is an alertable
    baseline series on the #683 ``cache.*`` surface even at 0 (mirrors
    ``BREAKER_OPEN_STAT_KEY`` / ``EVENT_LOOP_LAG_STAT_KEY``)."""

    def test_key_is_seeded_in_extra_keys(self):
        from lookup.router import _LML_CACHE_STATS_EXTRA_KEYS

        assert ADMISSION_WOULD_SHED_STAT_KEY in _LML_CACHE_STATS_EXTRA_KEYS
