"""Read-path unit tests for the verified library-release override (LML#850).

``fetch_artwork_for_items`` receives a ``release_overrides`` map
(``library_id -> discogs_release_id``) prefetched once per request by the
orchestrator. When a library row carries an override, ``fetch_one`` binds the
pinned release BEFORE the LML#604 trust-bind and the LML#478 artist-floor
fuzzy search — the human-verified pin is the most-trusted signal.

PG / the prefetch itself is not exercised here (see
``test_library_release_override_read.py`` for the query helper and the
orchestrator tests for the flag gate); these tests drive the artwork binder
with an explicit map, exactly as the orchestrator would after a flag-on
prefetch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import DiscogsSearchResponse, ReleaseMetadataResponse
from lookup.artwork import fetch_artwork_for_items
from lookup.release_resolution import ResolvedRelease
from tests.factories import make_discogs_result, make_library_item

# The verified pin: library row 42 -> Discogs release 5000.
_OVERRIDE_LIBRARY_ID = 42
_OVERRIDE_RELEASE_ID = 5000


def _svc_with_override_release() -> AsyncMock:
    """A Discogs service whose ``get_release`` resolves the *pinned* release.

    ``_bind_resolved_release`` fetches artwork for the bound release via
    ``_resolve_fallback_artwork`` -> ``get_release``, so the mock returns the
    pinned release's cover. ``search`` is stubbed to return a *different*
    (wrong) release so a test can prove the override wins over the fuzzy pick.
    """
    svc = AsyncMock()
    svc.cache_service = None
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=_OVERRIDE_RELEASE_ID,
            title="On Your Own Love Again",
            artist="Jessica Pratt",
            release_url=f"https://www.discogs.com/release/{_OVERRIDE_RELEASE_ID}",
            artwork_url="https://i.discogs.com/pinned.jpg",
        )
    )
    # The fuzzy path, if it ran, would bind this WRONG sibling release.
    wrong = make_discogs_result(
        release_id=99999,
        album="On Your Own Love Again",
        artist="Jessica Pratt",
        artwork_url="https://example.com/wrong.jpg",
    )
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[wrong]))
    return svc


@pytest.mark.asyncio
class TestOverrideBeatsFuzzy:
    async def test_override_binds_pinned_release_not_fuzzy_pick(self):
        item = make_library_item(
            id=_OVERRIDE_LIBRARY_ID,
            artist="Jessica Pratt",
            title="On Your Own Love Again",
        )
        svc = _svc_with_override_release()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            release_overrides={_OVERRIDE_LIBRARY_ID: _OVERRIDE_RELEASE_ID},
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == _OVERRIDE_RELEASE_ID
        # The fuzzy search must be short-circuited entirely — no Discogs
        # ``search`` call on an override hit (the per-request-work reduction).
        svc.search.assert_not_awaited()

    async def test_no_override_falls_through_to_fuzzy(self):
        # A library row WITHOUT a pin resolves by the fuzzy pick exactly as
        # before — the override map is consulted, misses, and behavior is
        # byte-for-byte unchanged.
        item = make_library_item(
            id=_OVERRIDE_LIBRARY_ID,
            artist="Jessica Pratt",
            title="On Your Own Love Again",
        )
        svc = _svc_with_override_release()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            release_overrides={},  # no pin for id 42
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 99999  # the fuzzy pick
        svc.search.assert_awaited()

    async def test_override_none_is_unchanged_behavior(self):
        # ``release_overrides=None`` (the default; flag off / no prefetch) is
        # identical to an empty map: fuzzy pick, no crash.
        item = make_library_item(
            id=_OVERRIDE_LIBRARY_ID,
            artist="Jessica Pratt",
            title="On Your Own Love Again",
        )
        svc = _svc_with_override_release()

        results = await fetch_artwork_for_items([item], svc)

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 99999


@pytest.mark.asyncio
class TestOverrideBeatsCarriedRelease:
    async def test_override_wins_over_a_carried_trust_bind(self, monkeypatch):
        # Even when the search strategy carried a validated release on the
        # ``discogs_titles`` seam (which the LML#604 trust-bind would otherwise
        # bind), the human-verified override must win. Enable the compilation
        # trust-bind flag so the carried release is a live competitor.
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            item = make_library_item(
                id=_OVERRIDE_LIBRARY_ID,
                artist="Jessica Pratt",
                title="On Your Own Love Again",
            )
            carried = ResolvedRelease(
                release_id=77777,  # the carried (non-override) release
                release_url="https://www.discogs.com/release/77777",
                is_compilation=False,
                album_title="On Your Own Love Again",
            )
            svc = _svc_with_override_release()

            results = await fetch_artwork_for_items(
                [item],
                svc,
                {_OVERRIDE_LIBRARY_ID: carried},  # discogs_titles seam
                release_overrides={_OVERRIDE_LIBRARY_ID: _OVERRIDE_RELEASE_ID},
            )

            bound = results[0][1]
            assert bound is not None
            assert bound.release_id == _OVERRIDE_RELEASE_ID
        finally:
            get_settings.cache_clear()
