"""Unit tests for core/service_unavailable.py and the LML#1036 503-detail
consolidation.

The consolidation's whole point is CONSTRUCTION, not CONTENT: every existing
503 detail string must keep its exact wire bytes even though it is now built
from `service_unavailable_detail(...)` instead of hand-typed inline. Each
class below pins one call site's exact byte value against the literal that
shipped before this refactor.
"""

from __future__ import annotations

from core.service_unavailable import service_unavailable_detail


class TestServiceUnavailableDetail:
    def test_builds_the_is_not_available_shape(self):
        assert (
            service_unavailable_detail("Discogs cache", "Set DATABASE_URL_DISCOGS.")
            == "Discogs cache is not available. Set DATABASE_URL_DISCOGS."
        )

    def test_service_and_remedy_are_both_required_positionally_or_by_keyword(self):
        assert (
            service_unavailable_detail(service="Widget service", remedy="Set WIDGET_TOKEN.")
            == "Widget service is not available. Set WIDGET_TOKEN."
        )


class TestDetailBytesUnchanged:
    """Pins the exact byte value each consolidated constant produces, matching
    what shipped on `main` before LML#1036 -- a regression test for
    "consolidate construction, not content."""

    def test_entity_store_unavailable_detail(self):
        from identity.dependencies import _ENTITY_STORE_UNAVAILABLE_DETAIL

        assert _ENTITY_STORE_UNAVAILABLE_DETAIL == (
            "Entity store is not available. Ensure DATABASE_URL_DISCOGS is configured "
            "and the entity schema has been applied."
        )

    def test_cache_router_discogs_service_unavailable_detail(self):
        from cache.router import _DISCOGS_SERVICE_UNAVAILABLE_DETAIL

        assert _DISCOGS_SERVICE_UNAVAILABLE_DETAIL == (
            "Discogs service is not available. Ensure DISCOGS_TOKEN (or "
            "DISCOGS_API_KEY / DISCOGS_API_SECRET) is configured."
        )

    def test_discogs_router_cache_unavailable_detail(self):
        from discogs.router import _DISCOGS_CACHE_UNAVAILABLE_DETAIL

        assert _DISCOGS_CACHE_UNAVAILABLE_DETAIL == (
            "Discogs cache is not available. Set DATABASE_URL_DISCOGS."
        )

    def test_artists_router_discogs_cache_unavailable_detail(self):
        from artists.router import _DISCOGS_CACHE_UNAVAILABLE_DETAIL

        assert _DISCOGS_CACHE_UNAVAILABLE_DETAIL == (
            "Discogs cache is not available. Ensure DATABASE_URL_DISCOGS is configured."
        )

    def test_discogs_router_service_unavailable_detail_is_the_deliberate_holdout(self):
        """This one is phrased "is not configured", not "is not available" --
        it does not fit `service_unavailable_detail`'s shape and is kept as a
        hand-written literal per LML#1036 rather than have its bytes changed
        to match the template."""
        from discogs.router import _DISCOGS_SERVICE_UNAVAILABLE_DETAIL

        assert _DISCOGS_SERVICE_UNAVAILABLE_DETAIL == (
            "Discogs service is not configured. Set DISCOGS_TOKEN environment variable."
        )
