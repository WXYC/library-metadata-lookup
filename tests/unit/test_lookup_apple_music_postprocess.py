"""Unit tests for the /lookup Apple Music post-process step.

``apply_apple_music_postprocess`` is the orchestrator's hook for the
"resolve Apple Music regardless of library status" guarantee. It runs
inside ``enrich_one`` after the existing per-item enrichment has built
the ``update`` dict; if the dict's ``apple_music_url`` came out null AND
the feature flag is on AND the request has artist + album, the function
calls the cache-backed Apple Music resolver and mutates the dict in
place.

Side effects:
* Cache write through ``resolve_apple_music_url_with_cache``.
* Sentry attribute on the active transaction:
  ``apple_music.persistent_lookup.fired = True`` (per-item outcome is
  intentionally NOT tagged on the transaction — concurrent ``enrich_one``
  tasks would race the ``set_data`` key).

Mint into ``entity.release_identity`` is intentionally not invoked here
until ``apple_music_album`` is wired through ``identity.release_validation``.
The test list reflects that: there are no mint assertions in this file.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import ResolveOutcome
from entity.sources import PgSource
from entity.store import EntityStore
from lookup.apple_music_postprocess import apply_apple_music_postprocess


def _make_entity_store():
    """Build an EntityStore stub that exposes a PgSource AsyncMock."""
    store = MagicMock(spec=EntityStore)
    store.pg = AsyncMock(spec=PgSource)
    return store


@pytest.mark.asyncio
class TestApplyAppleMusicPostprocess:
    async def test_skips_when_feature_flag_off(self):
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=False,
            )

        assert update["apple_music_url"] is None
        resolve.assert_not_called()

    async def test_skips_when_apple_music_url_already_set(self):
        # Existing enrichment populated the URL; don't double-process.
        update = {"apple_music_url": "https://music.apple.com/us/album/existing/1"}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == "https://music.apple.com/us/album/existing/1"
        resolve.assert_not_called()

    async def test_skips_when_apple_music_url_is_empty_string_sentinel(self):
        # An empty-string sentinel ("explicitly checked, nothing to surface")
        # must be preserved. Truthiness checks would treat "" as falsy and
        # overwrite it with a fresh probe; ``is not None`` correctly leaves
        # it alone.
        update = {"apple_music_url": ""}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == ""
        resolve.assert_not_called()

    async def test_skips_when_apple_music_client_missing(self):
        update = {"apple_music_url": None}
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=None,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        resolve.assert_not_called()

    async def test_skips_when_entity_store_missing(self):
        # No PG → no place to cache; degrade to no-op so the request still
        # succeeds (the existing per-item probe already ran).
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=None,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        resolve.assert_not_called()

    async def test_skips_when_request_artist_missing(self):
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        resolve.assert_not_called()

    async def test_skips_when_request_album_missing(self):
        # Artist-only lookups can't seed an (artist, album) cache row.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch("lookup.apple_music_postprocess.resolve_apple_music_url_with_cache") as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album=None,
                feature_enabled=True,
            )

        resolve.assert_not_called()

    async def test_live_resolved_writes_url_to_update(self):
        # Hyd-shape happy path: request values reach the probe and the URL
        # surfaces in the update dict.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        resolved_url = "https://music.apple.com/us/album/foo/1234567890"

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=resolved_url, source="live_resolved")),
        ) as resolve:
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == resolved_url
        # Resolver called with REQUEST values.
        resolve.assert_awaited_once()
        call_kwargs = resolve.await_args.kwargs
        assert call_kwargs["artist"] == "Hyd"
        assert call_kwargs["album"] == "Hold Onto Me Infinity"
        # probe_timeout_s passed through — wall-clock ceiling.
        assert "probe_timeout_s" in call_kwargs
        assert call_kwargs["probe_timeout_s"] > 0

    async def test_cache_hit_sets_url(self):
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        cached_url = "https://music.apple.com/us/album/foo/9999999"

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=cached_url, source="cache_hit")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == cached_url

    async def test_live_miss_leaves_url_null(self):
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=None, source="live_miss")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] is None

    async def test_live_error_leaves_url_null(self):
        # Live_error is distinct from live_miss but the post-process
        # treats both the same way at the call site: surface no URL.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=None, source="live_error")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] is None
