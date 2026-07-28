"""LML#931: traffic-class observability — caller-reason slice.

Reads the caller-supplied ``X-Caller-Reason`` header (forwarded by
Backend-Service since BS#1843, e.g. ``proxy-library-search`` or
``catalog-popularity-freetext-resolve``) and emits it as a
``lml.caller_reason`` Sentry tag plus a matching PostHog ``caller_reason``
property, recorded once per request/batch context right after
``record_endpoint_family_tag`` — the same call-site pattern as the #944
``endpoint_family`` dimension this mirrors (see
``tests/unit/test_traffic_class_observability.py``).

Unlike ``endpoint_family`` (always one of two fixed literals), the header is
genuinely optional: older callers and any hop that predates BS#1843 send
nothing. Absence must be a safe no-op — no tag, no PostHog property, never a
crash, never a fabricated value — so every "header present" test here has a
paired "header absent" test.

The other #931 lag dimensions (``admission-result``, ``degraded-mode-result``,
``timeout-phase``) stay out of scope, gated on #930/#943.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from lookup.caller_reason import record_caller_reason_tag
from lookup.models import LookupResponse
from main import app
from tests.factories import LOOKUP_BODY
from tests.unit.conftest import override_deps


class TestRecordCallerReasonTag:
    """Direct unit coverage for the LML#931 Sentry-tag helper.

    Mirrors ``TestRecordEndpointFamilyTag``'s shape, plus the ``None``-guard
    that ``endpoint_family`` doesn't need (its two values are always
    fixed literals, never absent).
    """

    def test_records_tag_when_reason_present(self):
        set_tag = Mock()
        with patch("lookup.caller_reason.sentry_sdk.set_tag", set_tag):
            record_caller_reason_tag("proxy-library-search")

        set_tag.assert_called_once_with("lml.caller_reason", "proxy-library-search")

    def test_no_op_when_reason_absent(self):
        """``None`` (header missing) must not touch the Sentry SDK at all."""
        set_tag = Mock()
        with patch("lookup.caller_reason.sentry_sdk.set_tag", set_tag):
            record_caller_reason_tag(None)

        set_tag.assert_not_called()

    def test_swallows_sdk_errors(self):
        """Any SDK-side exception is swallowed so observability cannot break /lookup."""
        with patch(
            "lookup.caller_reason.sentry_sdk.set_tag",
            side_effect=RuntimeError("sentry exploded"),
        ):
            record_caller_reason_tag("proxy-library-search")  # must not raise


def _completed_properties(mock_posthog: Mock) -> dict:
    """Pull the full properties dict off the PostHog ``*_completed`` event."""
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
    headers: dict[str, str] | None = None,
) -> None:
    """POST to a lookup endpoint with ``perform_lookup``, PostHog, and
    ``sentry_sdk.set_tag`` all mocked. Mirrors ``_post`` in
    ``test_traffic_class_observability.py``, scoped to the
    ``lookup.caller_reason`` tag instead of ``lookup.endpoint_family``.
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
        patch("lookup.caller_reason.sentry_sdk.set_tag", set_tag),
        patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=LookupResponse(results=[], search_type="none"),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(endpoint, json=json_body, headers=headers or {})

    assert resp.status_code == 200


class TestCallerReasonWiredIntoHandlers:
    """End-to-end (via ASGI client): both handlers thread ``X-Caller-Reason``
    into the ``lml.caller_reason`` Sentry tag and the PostHog
    ``*_completed`` event's ``caller_reason`` property when present, and
    emit neither when the header is absent (LML#931)."""

    @pytest.mark.asyncio
    async def test_lookup_tags_and_posthog_property_when_header_present(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup",
            LOOKUP_BODY,
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
            headers={"X-Caller-Reason": "proxy-library-search"},
        )

        # `set_tag` is shared with `record_endpoint_family_tag` (both patch the
        # same underlying `sentry_sdk` module), so both handlers' calls land on
        # this one mock -- assert membership, mirroring
        # `TestEndpointFamilyWiredIntoHandlers` in test_traffic_class_observability.py.
        assert ("lml.caller_reason", "proxy-library-search") in [
            c.args for c in set_tag.call_args_list
        ]
        assert _completed_properties(mock_posthog)["caller_reason"] == "proxy-library-search"

    @pytest.mark.asyncio
    async def test_lookup_omits_tag_and_property_when_header_absent(self, mock_settings):
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

        # `record_endpoint_family_tag` still fires unconditionally (out of
        # scope here), so assert absence of the *caller_reason* key specifically.
        assert "lml.caller_reason" not in {c.args[0] for c in set_tag.call_args_list}
        assert "caller_reason" not in _completed_properties(mock_posthog)

    @pytest.mark.asyncio
    async def test_bulk_lookup_tags_and_posthog_property_when_header_present(self, mock_settings):
        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        set_tag = Mock()

        await _post(
            "/api/v1/lookup/bulk",
            {"items": [{"artist": "Stereolab", "album": "Aluminum Tunes"}]},
            mock_posthog=mock_posthog,
            set_tag=set_tag,
            mock_settings=mock_settings,
            headers={"X-Caller-Reason": "catalog-popularity-freetext-resolve"},
        )

        assert ("lml.caller_reason", "catalog-popularity-freetext-resolve") in [
            c.args for c in set_tag.call_args_list
        ]
        assert (
            _completed_properties(mock_posthog)["caller_reason"]
            == "catalog-popularity-freetext-resolve"
        )

    @pytest.mark.asyncio
    async def test_bulk_lookup_omits_tag_and_property_when_header_absent(self, mock_settings):
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

        assert "lml.caller_reason" not in {c.args[0] for c in set_tag.call_args_list}
        assert "caller_reason" not in _completed_properties(mock_posthog)
