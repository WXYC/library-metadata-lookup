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
* ``EntityStore.mint_or_get_release_identity`` mint when a real URL is
  resolved (so ``entity.release_identity`` accumulates ``apple_music_album_id``s).
* Sentry attribute on the active transaction: ``apple_music.persistent_lookup.source``.
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
    store.mint_or_get_release_identity = AsyncMock(return_value=(1, True))
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
                request_song="Angel",
                feature_enabled=False,
            )

        assert update["apple_music_url"] is None
        resolve.assert_not_called()
        entity_store.mint_or_get_release_identity.assert_not_called()

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
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == "https://music.apple.com/us/album/existing/1"
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
                request_song="Angel",
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
                request_song="Angel",
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
                request_song="Angel",
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
                request_song="Angel",
                feature_enabled=True,
            )

        resolve.assert_not_called()

    async def test_live_resolved_writes_url_and_mints(self):
        # Hyd-shape happy path: request values reach the probe, URL surfaces
        # in the update dict, entity.release_identity gets the album_id.
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
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == resolved_url
        # Resolver called with REQUEST values.
        resolve.assert_awaited_once()
        call_kwargs = resolve.await_args.kwargs
        assert call_kwargs["artist"] == "Hyd"
        assert call_kwargs["album"] == "Hold Onto Me Infinity"
        assert call_kwargs["song"] == "Angel"
        # Album_id extracted from the URL is minted into release_identity.
        entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source="apple_music_album", external_id="1234567890"
        )

    async def test_cache_hit_sets_url_no_mint(self):
        # Cache already minted on the original resolution — re-minting on
        # every cache hit would double-write the reconciliation log.
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
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == cached_url
        entity_store.mint_or_get_release_identity.assert_not_called()

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
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] is None
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_mint_failure_does_not_clear_resolved_url(self):
        # Mint is best-effort: a PG error here must not undo the user-visible
        # win of having an Apple Music URL surfaced.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(side_effect=RuntimeError("PG flake"))
        resolved_url = "https://music.apple.com/us/album/foo/1234567890"

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=resolved_url, source="live_resolved")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == resolved_url

    async def test_unparseable_apple_url_does_not_attempt_mint(self):
        # Defensive: if Apple returned a URL that doesn't match the
        # album_id regex (slug-only, malformed locale), the mint step is
        # skipped silently. The URL still surfaces in the response so the
        # user gets the link.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        weird_url = "https://music.apple.com/album/foo"  # no locale + no id

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=weird_url, source="live_resolved")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                request_song="Angel",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == weird_url
        entity_store.mint_or_get_release_identity.assert_not_called()
