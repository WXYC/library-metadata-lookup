"""Orchestrator prefetch + flag-gate tests for the library-release override (LML#850).

``_prefetch_release_overrides`` is the flag gate: it reads the
``lml_library_release_override`` setting once, and only when it is on (and a
discogs-cache PG is configured and there are library rows) does it issue the
single-query prefetch. When off, it returns an empty map with zero PG work —
the "flag-off = unchanged behavior, zero added cost" acceptance criterion.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lookup.orchestrator import LookupState, _prefetch_release_overrides, _step_fetch_artwork
from tests.factories import make_library_item


def _services_stub(**overrides):
    """A minimal LookupServices stand-in for the _step_* wiring tests."""
    services = MagicMock()
    services.telemetry.track_step = MagicMock(return_value=contextlib.nullcontext())
    services.telemetry.record_api_call = MagicMock()
    services.discogs_cache_pg = object()
    services.discogs_service = object()
    services.allow_release_resolution_fallback = True
    for key, value in overrides.items():
        setattr(services, key, value)
    return services


def _parsed_stub():
    parsed = MagicMock()
    parsed.song = None
    parsed.album = "On Your Own Love Again"
    parsed.artist = "Jessica Pratt"
    return parsed


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setenv("LML_LIBRARY_RELEASE_OVERRIDE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _flag_off(monkeypatch):
    monkeypatch.setenv("LML_LIBRARY_RELEASE_OVERRIDE", "false")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
class TestPrefetchReleaseOverrides:
    async def test_flag_off_returns_empty_without_pg_query(self, _flag_off):
        pg = AsyncMock()
        pg.fetchall = AsyncMock(return_value=[])
        items = [make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")]

        result = await _prefetch_release_overrides(items, pg)

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_flag_on_prefetches_map(self, _flag_on):
        pg = AsyncMock()
        pg.fetchall = AsyncMock(return_value=[{"library_id": 42, "discogs_release_id": 5000}])
        items = [make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")]

        result = await _prefetch_release_overrides(items, pg)

        assert result == {42: 5000}
        assert pg.fetchall.await_count == 1

    async def test_flag_on_no_pg_returns_empty(self, _flag_on):
        items = [make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")]

        result = await _prefetch_release_overrides(items, None)

        assert result == {}

    async def test_flag_on_empty_results_returns_empty(self, _flag_on):
        pg = AsyncMock()
        pg.fetchall = AsyncMock(return_value=[])

        result = await _prefetch_release_overrides([], pg)

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_flag_on_skips_rowless_ids(self, _flag_on):
        # A synthesized row-less item (id=0) is never a real card-catalog id and
        # can carry no verified pin — it must not enter the prefetched id list.
        pg = AsyncMock()
        pg.fetchall = AsyncMock(return_value=[{"library_id": 42, "discogs_release_id": 5000}])
        items = [
            make_library_item(id=0, artist="Jessica Pratt", title="On Your Own Love Again"),
            make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again"),
        ]

        result = await _prefetch_release_overrides(items, pg)

        assert result == {42: 5000}
        queried_ids = pg.fetchall.await_args.args[1]
        assert 0 not in queried_ids

    async def test_bulk_kill_switch_skips_prefetch_even_with_flag_on(self, _flag_on):
        # /lookup/bulk passes allow_release_resolution_fallback=False; the override
        # prefetch must be skipped entirely (no per-item PG fan-out across the
        # shared discogs-cache pool), parity with every sibling bulk exclusion.
        pg = AsyncMock()
        pg.fetchall = AsyncMock(return_value=[{"library_id": 42, "discogs_release_id": 5000}])
        items = [make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")]

        result = await _prefetch_release_overrides(
            items, pg, allow_release_resolution_fallback=False
        )

        assert result == {}
        pg.fetchall.assert_not_awaited()


@pytest.mark.asyncio
class TestStepWiring:
    """Guards the orchestrator wiring: a refactor that drops the ``release_overrides``
    kwarg would silently kill the feature (the param defaults to None), so this
    asserts the prefetched map is forwarded to fetch_artwork_for_items, and the
    bulk kill switch reaches the prefetch."""

    async def test_step_fetch_artwork_forwards_override_map(self):
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        state = LookupState(library_results=[item])
        services = _services_stub()

        with (
            patch(
                "lookup.orchestrator._prefetch_release_overrides",
                new=AsyncMock(return_value={42: 5000}),
            ),
            patch(
                "lookup.orchestrator.fetch_artwork_for_items",
                new=AsyncMock(return_value=[(item, None)]),
            ) as mock_fetch,
        ):
            await _step_fetch_artwork(_parsed_stub(), state, services)

        assert mock_fetch.await_args.kwargs["release_overrides"] == {42: 5000}

    async def test_step_fetch_artwork_passes_bulk_kill_switch(self):
        # The per-request bulk flag must reach _prefetch_release_overrides so the
        # drain skips the prefetch.
        item = make_library_item(id=42, artist="Jessica Pratt", title="On Your Own Love Again")
        state = LookupState(library_results=[item])
        services = _services_stub(allow_release_resolution_fallback=False)

        with (
            patch(
                "lookup.orchestrator._prefetch_release_overrides",
                new=AsyncMock(return_value={}),
            ) as mock_prefetch,
            patch(
                "lookup.orchestrator.fetch_artwork_for_items",
                new=AsyncMock(return_value=[(item, None)]),
            ),
        ):
            await _step_fetch_artwork(_parsed_stub(), state, services)

        assert mock_prefetch.await_args.kwargs["allow_release_resolution_fallback"] is False
