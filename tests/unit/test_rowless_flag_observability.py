"""LML#681: pre-flip observability for ``LML_RESOLVE_NONLIBRARY_RELEASE``.

Three cheap counters/tags ride the existing ``cache_stats`` seam so the
``LML_RESOLVE_NONLIBRARY_RELEASE`` staging->prod flip is observable end-to-end
(both PostHog and the Sentry transaction project the all-numeric dict):

1. a per-request **flag tag** (``0``/``1``) recorded once per ``cache_stats``
   context at the router, so flag-on vs flag-off is sliceable;
2. a ``nonlibrary_release_surfaced`` counter at the single ``_make_rowless_item``
   chokepoint, deliberately scoped to the *flag-gated* row-less producers (the
   un-gated LML#583 library-miss ``id=0`` path is excluded);
3. ``release_resolution_cache_{hit,miss,unavailable}`` for the #632 PG positive
   cache, so repeat-add amortization is measurable.

Instrumentation only — no behavior change to the row-less producers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from config.settings import get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from lookup.models import LookupResponse
from lookup.router import LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY
from main import app
from tests.factories import LOOKUP_BODY
from tests.unit.conftest import override_deps


class TestNonlibraryReleaseSurfacedCounter:
    """The ``nonlibrary_release_surfaced`` counter fires once per flag-gated
    row-less ``LibraryItem(id=0)`` emission and only via ``_make_rowless_item``."""

    def test_make_rowless_item_increments_counter(self):
        """The single row-less chokepoint records one surfaced emission."""
        from lookup.rowless import (
            NONLIBRARY_RELEASE_SURFACED_STAT_KEY,
            _make_rowless_item,
        )

        init_cache_stats(extra_keys=(NONLIBRARY_RELEASE_SURFACED_STAT_KEY,))
        _make_rowless_item(artist="Stereolab", title="Aluminum Tunes")

        stats = get_cache_stats()
        assert stats[NONLIBRARY_RELEASE_SURFACED_STAT_KEY] == 1

    @pytest.mark.asyncio
    async def test_library_miss_id0_path_does_not_increment(self, mock_discogs_service):
        """The un-gated LML#583 library-miss search surfaces ``LibraryItem(id=0)``
        directly (not via ``_make_rowless_item``), so it must NOT bump this
        flag-scoped counter — else the "0 when flag off" property breaks and two
        independent features get conflated."""
        from discogs.models import DiscogsSearchResponse
        from lookup.orchestrator import _library_miss_discogs_search
        from lookup.rowless import NONLIBRARY_RELEASE_SURFACED_STAT_KEY
        from services.parser import MessageType, ParsedRequest
        from tests.factories import make_discogs_result

        # A candidate that clears the 80/80 floor → the helper synthesizes its
        # own id=0 item, exercising the exact path that must stay uncounted.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )

        init_cache_stats(extra_keys=(NONLIBRARY_RELEASE_SURFACED_STAT_KEY,))
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)

        assert result is not None and result[0].id == 0  # the #583 id=0 surface
        stats = get_cache_stats()
        assert stats[NONLIBRARY_RELEASE_SURFACED_STAT_KEY] == 0


class TestReleaseResolutionCacheCounters:
    """``get_cached_release_id`` returns four outcomes; the hit/miss/unavailable
    counters map them deliberately so a PG outage can't inflate the miss rate."""

    @staticmethod
    def _seed():
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
        )

        init_cache_stats(
            extra_keys=(
                RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
                RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
                RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
            )
        )

    @pytest.mark.asyncio
    async def test_absent_row_records_miss_only(self):
        """``row is None`` (absent / stale-positive / stale-miss) → one miss."""
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            get_cached_release_id,
        )

        pg = AsyncMock()
        pg.fetchone = AsyncMock(return_value=None)
        self._seed()

        result = await get_cached_release_id(
            pg, artist="Stereolab", title="Aluminum Tunes", is_track=False
        )

        assert result.was_present is False
        stats = get_cache_stats()
        assert stats[RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY] == 1
        assert stats[RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY] == 0

    @pytest.mark.asyncio
    async def test_fresh_positive_id_records_hit_only(self):
        """``was_present=True`` with a positive id → one hit, no miss."""
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            get_cached_release_id,
        )

        pg = AsyncMock()
        pg.fetchone = AsyncMock(return_value={"release_id": 37008771})
        self._seed()

        result = await get_cached_release_id(
            pg, artist="Stereolab", title="Aluminum Tunes", is_track=False
        )

        assert result.was_present is True and result.release_id == 37008771
        stats = get_cache_stats()
        assert stats[RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY] == 1
        assert stats[RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY] == 0

    @pytest.mark.asyncio
    async def test_fresh_known_miss_shortcircuit_records_hit(self):
        """A fresh known miss (``release_id=None, was_present=True``) still
        avoids the live probe, so it counts as a hit — recorded on
        ``was_present``, NOT on a non-null release_id. This is the amortization
        win the counter is meant to prove."""
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            get_cached_release_id,
        )

        pg = AsyncMock()
        pg.fetchone = AsyncMock(return_value={"release_id": None})
        self._seed()

        result = await get_cached_release_id(
            pg, artist="Stereolab", title="Aluminum Tunes", is_track=False
        )

        assert result.was_present is True and result.release_id is None
        stats = get_cache_stats()
        assert stats[RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY] == 1
        assert stats[RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY] == 0

    @pytest.mark.asyncio
    async def test_pg_exception_records_unavailable_not_miss(self):
        """The PG-exception branch is a degradation, not a cache miss — it must
        increment only ``..._unavailable`` so an outage can't inflate the miss
        rate while the dashboard is read."""
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
            get_cached_release_id,
        )

        pg = AsyncMock()
        pg.fetchone = AsyncMock(side_effect=Exception("pg down"))
        self._seed()

        result = await get_cached_release_id(
            pg, artist="Stereolab", title="Aluminum Tunes", is_track=False
        )

        assert result.was_present is False
        stats = get_cache_stats()
        assert stats[RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY] == 1
        assert stats[RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY] == 0
        assert stats[RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY] == 0


def _completed_cache_props(mock_posthog: Mock) -> dict:
    """Pull the ``cache`` property off the PostHog ``*_completed`` event.

    ``RequestTelemetry.send_to_posthog`` reads the raw ``cache_stats`` dict (not
    the strict ``CacheStats`` response model, which drops the extra keys), so the
    flag tag is visible here and in the Sentry transaction projection."""
    for call in mock_posthog.capture.call_args_list:
        event = call.kwargs.get("event", "")
        if event.endswith("_completed"):
            return call.kwargs["properties"]["cache"]
    raise AssertionError("no *_completed PostHog event captured")


async def _post_for_cache_props(endpoint: str, json_body: dict, *, mock_settings) -> dict:
    """POST to a lookup endpoint with ``perform_lookup`` and PostHog mocked, then
    return the ``*_completed`` event's ``cache`` props.

    Shared by the flag-tag cases so the override + patch + client scaffolding
    lives once. The flag value under test is set by the ``set_flags`` fixture
    (env + ``get_settings.cache_clear``) — the router reads ``get_settings()``
    directly, so env is the control point; the ``get_settings`` override here
    only neutralizes the DI-injected settings for the other dependency providers,
    matching the sibling router suites."""
    mock_posthog = Mock()
    mock_posthog.capture = Mock()
    mock_posthog.flush = Mock()

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
        patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=LookupResponse(results=[], search_type="none"),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(endpoint, json=json_body)

    assert resp.status_code == 200
    return _completed_cache_props(mock_posthog)


@pytest.fixture
def set_flags(monkeypatch):
    """Set the row-less-family flags via env and reset the lru_cache around the
    test, mirroring the orchestrator suite's flag toggling. The router reads the
    process-constant ``get_settings()`` directly, so env is the control point."""

    def _apply(*, nonlibrary: bool, compilation: bool = False):
        monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true" if nonlibrary else "false")
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true" if compilation else "false")
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield _apply
    get_settings.cache_clear()


class TestFlagTag:
    """The flag tag rides ``cache_stats`` as a clean ``0``/``1`` on BOTH
    endpoints — recorded once per context at the router so the shared-context
    bulk batch never sums it to the batch size."""

    @pytest.mark.parametrize("nonlibrary, expected", [(True, 1), (False, 0)])
    @pytest.mark.asyncio
    async def test_lookup_records_flag_state(self, nonlibrary, expected, mock_settings, set_flags):
        """``/lookup`` carries the flag as ``1`` when on, ``0`` when off (the
        flag-off baseline the A/B comparison needs)."""
        set_flags(nonlibrary=nonlibrary)
        cache = await _post_for_cache_props(
            "/api/v1/lookup", LOOKUP_BODY, mock_settings=mock_settings
        )
        assert cache[LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY] == expected

    @pytest.mark.asyncio
    async def test_bulk_flag_tag_is_one_not_summed_across_batch(self, mock_settings, set_flags):
        """The discriminating "not summed" guard: with the flag ON and a 3-item
        batch sharing ONE cache_stats context, the tag must be ``1`` (recorded
        once at the router), never ``3``. Recording it per-item in the
        orchestrator would sum to the batch size."""
        set_flags(nonlibrary=True)
        cache = await _post_for_cache_props(
            "/api/v1/lookup/bulk",
            {
                "items": [
                    {"artist": "Stereolab", "album": "Aluminum Tunes"},
                    {"artist": "Juana Molina", "album": "DOGA"},
                    {"artist": "Cat Power", "album": "Moon Pix"},
                ]
            },
            mock_settings=mock_settings,
        )
        assert cache[LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY] == 1


class TestPayloadShapeStability:
    """Every new key is seeded into ``_LML_CACHE_STATS_EXTRA_KEYS`` so the
    PostHog/Sentry payload shape stays stable across requests (LML#544)."""

    def test_all_new_keys_seeded(self):
        from entity.release_resolution_cache import (
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
        )
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            LML_RESOLVE_COMPILATION_RELEASE_STAT_KEY,
            LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
        )
        from lookup.rowless import NONLIBRARY_RELEASE_SURFACED_STAT_KEY

        for key in (
            LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
            LML_RESOLVE_COMPILATION_RELEASE_STAT_KEY,
            NONLIBRARY_RELEASE_SURFACED_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
            RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
        ):
            assert key in _LML_CACHE_STATS_EXTRA_KEYS

    def test_seeded_keys_default_to_zero(self):
        """A request that records nothing still emits every new key as ``0`` —
        the shape guarantee that keeps flag-off the comparison baseline."""
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
        )

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        stats = get_cache_stats()
        assert stats[LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY] == 0
