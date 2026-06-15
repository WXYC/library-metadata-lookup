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

Mint into ``entity.release_identity.apple_music_album_id`` fires on
``live_resolved`` outcomes. The mint is best-effort: failures (PG
errors, validation rejection) log and continue; the URL must still
surface in the response. Cache hits do NOT re-mint — the original
resolution already minted; re-minting writes redundant
reconciliation log rows for no benefit. See ``apply_apple_music_postprocess``
in ``lookup/apple_music_postprocess.py`` for the contract.
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


@pytest.mark.asyncio
class TestApplyAppleMusicPostprocessMint:
    """Mint side-effect into ``entity.release_identity.apple_music_album_id``.

    The post-process extracts the album_id from the resolved URL and calls
    ``EntityStore.mint_or_get_release_identity("apple_music_album", album_id)``
    only on ``live_resolved`` outcomes. Cache hits do NOT re-mint — the
    original resolution already minted, and re-minting writes redundant
    reconciliation rows. Other outcomes (``cache_miss_recent`` / ``live_miss``
    / ``live_error``) have no URL to mint from.

    Mint is best-effort: failures log + continue; the URL must still
    surface on ``update["apple_music_url"]``.
    """

    async def test_live_resolved_mints_album_id(self):
        # Live_resolved outcome with a parseable URL → mint fires with the
        # parsed album_id, URL surfaces in update.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))
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
                feature_enabled=True,
            )

        assert update["apple_music_url"] == resolved_url
        entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source="apple_music_album", external_id="1234567890"
        )

    async def test_cache_hit_does_not_re_mint(self):
        # Cache hits already minted on their original resolution; the
        # post-process must NOT re-mint to avoid redundant reconciliation
        # log rows.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, False))
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
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_live_miss_does_not_mint(self):
        # No URL → no album_id to mint.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))

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

        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_live_error_does_not_mint(self):
        # Apple raised / timed out → no URL → no mint.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))

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

        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_cache_miss_recent_does_not_mint(self):
        # cache_miss_recent: cache says "we checked recently, no URL". No URL
        # to mint from.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=None, source="cache_miss_recent")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_mint_failure_is_swallowed_and_url_still_surfaces(self):
        # Best-effort mint: if mint_or_get_release_identity raises, the URL
        # must still surface on the update dict. The mint side-effect is
        # observability/graph enrichment — never a request blocker.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(
            side_effect=RuntimeError("PG timeout")
        )
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
                feature_enabled=True,
            )

        # Mint was attempted and raised; URL still surfaced.
        entity_store.mint_or_get_release_identity.assert_awaited_once()
        assert update["apple_music_url"] == resolved_url

    async def test_unparseable_url_skips_mint_but_surfaces_url(self):
        # Apple returned a URL we can't parse for an album_id (slug-only,
        # malformed locale, novel format). Surface the URL but don't mint —
        # keying the entity graph on an unparseable ID would corrupt
        # downstream joins.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))
        # URL that doesn't match the album_id regex (no trailing numeric ID).
        unparseable_url = "https://music.apple.com/us/album/foo"

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=unparseable_url, source="live_resolved")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == unparseable_url
        entity_store.mint_or_get_release_identity.assert_not_called()
