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
* Sentry attributes on the active transaction (LML#575):
  - ``apple_music.persistent_lookup.fired = True`` — "post-process ran at
    least once on this request" (back-compat boolean).
  - ``apple_music.persistent_lookup.<source> = True`` — per-outcome
    boolean, one of ``cache_hit``, ``cache_miss_recent``, ``live_resolved``,
    ``live_miss``, ``live_error``. Each invocation sets one outcome key;
    across the gather fan-out, multiple keys can land on the same
    transaction (one per distinct outcome observed). Race-free because
    different keys never overwrite each other, and the same key set to
    ``True`` repeatedly is idempotent.

Mint into ``entity.release_identity.apple_music_album_id`` fires on
``live_resolved`` outcomes. The mint is best-effort: failures (PG
errors, validation rejection) log and continue; the URL must still
surface in the response. Cache hits do NOT re-mint — the original
resolution already minted; re-minting writes redundant
reconciliation log rows for no benefit. See ``apply_apple_music_postprocess``
in ``lookup/apple_music_postprocess.py`` for the contract.
"""

from __future__ import annotations

import asyncio
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import ResolveOutcome, ResolveSource
from entity.sources import PgSource
from entity.store import EntityStore
from lookup.apple_music_postprocess import apply_apple_music_postprocess

_RESOLVE_SOURCE_VALUES: set[str] = set(get_args(ResolveSource))


def _make_entity_store():
    """Build an EntityStore stub that exposes a PgSource AsyncMock."""
    store = MagicMock(spec=EntityStore)
    store.pg = AsyncMock(spec=PgSource)
    return store


def _make_sentry_scope():
    """Build a Sentry scope + transaction pair that records every set_data."""
    transaction = Mock()
    transaction.set_data = Mock()
    scope = Mock()
    scope.transaction = transaction
    return scope, transaction


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

    async def test_zero_padded_id_is_validated_before_mint_and_url_still_surfaces(self):
        # Defense-in-depth: the URL parser regex ``\d{6,}`` admits
        # all-zero / leading-zero IDs that the validator's
        # ``[1-9][0-9]*`` rule rejects. The post-process must call
        # ``validate_and_canonicalize_external_id`` BEFORE the mint so a
        # poisoned id never lands in entity.release_identity. Validation
        # rejection is a best-effort failure mode (same posture as the
        # PG-outage and unparseable-URL paths): the URL must still
        # surface on the update dict. This test would pass-by-coincidence
        # if the validator were skipped — the assertion that mint is
        # NOT called is what locks the contract in.
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(42, True))
        # URL whose album_id is "000000" — passes the URL parser's
        # `\d{6,}` floor but the validator rejects on leading zero.
        zero_padded_url = "https://music.apple.com/us/album/foo/000000"

        with patch(
            "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=zero_padded_url, source="live_resolved")),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        assert update["apple_music_url"] == zero_padded_url
        entity_store.mint_or_get_release_identity.assert_not_called()


_PARAMETRIZED_SOURCES: list[tuple[str, str | None]] = [
    ("cache_hit", "https://music.apple.com/us/album/foo/9999999"),
    ("cache_miss_recent", None),
    ("live_resolved", "https://music.apple.com/us/album/foo/1234567890"),
    ("live_miss", None),
    ("live_error", None),
]


def test_parametrized_sources_cover_every_resolve_source_variant():
    """Completeness guard for the parametrized Sentry-tag test.

    The parametrize list is hand-maintained. If a new value is added to
    ``ResolveSource`` (e.g. a future ``live_throttled`` for 429 storms) the
    parametrized cases below would silently keep passing without exercising
    the new variant. This assertion fails fast in that case and forces a
    follow-up to add the missing parametrize tuple.
    """
    parametrized = {source for source, _ in _PARAMETRIZED_SOURCES}
    assert parametrized == _RESOLVE_SOURCE_VALUES, (
        f"Parametrize list drifted from ResolveSource. "
        f"Missing from parametrize: {_RESOLVE_SOURCE_VALUES - parametrized}; "
        f"extra in parametrize: {parametrized - _RESOLVE_SOURCE_VALUES}"
    )


@pytest.mark.asyncio
class TestPersistentLookupSentryTags:
    """LML#575: each ResolveOutcome.source projects a per-outcome boolean
    tag onto the active Sentry transaction, alongside the back-compat
    ``apple_music.persistent_lookup.fired = True`` boolean.

    Why per-outcome: PR #571's single ``fired`` boolean only answered "did
    the post-process run at all" but dashboards need cache hit rate, live
    error rate (Apple outage signal), and live-call cost. Same `set_data`
    pattern as ``lookup/orchestrator.py:3013-3022``. Concurrent
    ``enrich_one`` tasks setting the same key with the same value (True)
    are race-free.
    """

    @pytest.mark.parametrize(("source", "resolved_url"), _PARAMETRIZED_SOURCES)
    async def test_per_outcome_boolean_set_for_each_source(self, source, resolved_url):
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        scope, transaction = _make_sentry_scope()

        with (
            patch(
                "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
                new=AsyncMock(return_value=ResolveOutcome(url=resolved_url, source=source)),
            ),
            patch(
                "lookup.apple_music_postprocess.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        # Back-compat: the original boolean still fires.
        assert calls.get("apple_music.persistent_lookup.fired") is True
        # New: a per-outcome boolean for the source the resolver returned.
        assert calls.get(f"apple_music.persistent_lookup.{source}") is True
        # Per-invocation isolation: a single ``apply_apple_music_postprocess``
        # call must only set the key for its own outcome. The cross-item case
        # (multiple ``enrich_one`` invocations on the same request setting
        # different keys → all five could end ``True``) is the intended
        # production behavior and is covered in
        # ``test_concurrent_invocations_set_per_outcome_keys_idempotently``.
        for other in _RESOLVE_SOURCE_VALUES - {source}:
            assert f"apple_music.persistent_lookup.{other}" not in calls

    async def test_per_outcome_tag_survives_no_active_transaction(self):
        """When the SDK has no active transaction, the projection is a
        no-op — same swallow contract as the existing ``fired`` boolean.
        """
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        scope = Mock()
        scope.transaction = None

        with (
            patch(
                "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
                new=AsyncMock(
                    return_value=ResolveOutcome(
                        url="https://music.apple.com/us/album/foo/1",
                        source="live_resolved",
                    )
                ),
            ),
            patch(
                "lookup.apple_music_postprocess.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        # User-visible URL still set; the missing transaction must not crash.
        assert update["apple_music_url"] == "https://music.apple.com/us/album/foo/1"

    async def test_per_outcome_tag_failure_does_not_break_request(self):
        """If ``set_data`` raises, the request must still succeed. Same
        observability-must-not-break-request contract as the existing
        ``fired`` boolean and the orchestrator cache-stats projection.
        """
        update = {"apple_music_url": None}
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()
        transaction = Mock()
        transaction.set_data = Mock(side_effect=RuntimeError("boom"))
        scope = Mock()
        scope.transaction = transaction

        with (
            patch(
                "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
                new=AsyncMock(
                    return_value=ResolveOutcome(
                        url="https://music.apple.com/us/album/foo/1",
                        source="live_resolved",
                    )
                ),
            ),
            patch(
                "lookup.apple_music_postprocess.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            await apply_apple_music_postprocess(
                update,
                apple_music=apple_music,
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                feature_enabled=True,
            )

        # URL still applied — observability failure doesn't roll back the
        # resolution result.
        assert update["apple_music_url"] == "https://music.apple.com/us/album/foo/1"

    async def test_concurrent_invocations_set_per_outcome_keys_idempotently(self):
        """Cross-item fan-out: ``enrich_one`` runs N apply calls in
        ``asyncio.gather``; different items can return different outcomes
        and each item's invocation can independently project its outcome
        key onto the *shared* transaction.

        The race-free claim is two-fold:
        1. Different keys never overwrite each other (the keys are
           disjoint, one per ``ResolveSource`` variant).
        2. The same key set repeatedly to ``True`` is idempotent — two
           items with the same outcome don't clobber the boolean.
        """
        scope, transaction = _make_sentry_scope()
        apple_music = AsyncMock(spec=AppleMusicClient)
        entity_store = _make_entity_store()

        # Two items resolved with different outcomes; one resolved with the
        # same outcome as the first (the idempotent re-set case).
        outcomes = [
            ResolveOutcome(url="https://music.apple.com/us/album/a/1", source="cache_hit"),
            ResolveOutcome(url=None, source="live_error"),
            ResolveOutcome(url="https://music.apple.com/us/album/c/3", source="cache_hit"),
        ]
        # Each gather task gets its own update dict — ``apply`` mutates in
        # place, and items don't share state in prod either.
        updates = [{"apple_music_url": None} for _ in outcomes]
        resolver_results = iter(outcomes)

        async def fake_resolve(*args, **kwargs):
            return next(resolver_results)

        with (
            patch(
                "lookup.apple_music_postprocess.resolve_apple_music_url_with_cache",
                new=AsyncMock(side_effect=fake_resolve),
            ),
            patch(
                "lookup.apple_music_postprocess.sentry_sdk.get_current_scope",
                return_value=scope,
            ),
        ):
            await asyncio.gather(
                *(
                    apply_apple_music_postprocess(
                        u,
                        apple_music=apple_music,
                        entity_store=entity_store,
                        request_artist="Hyd",
                        request_album="Hold Onto Me Infinity",
                        feature_enabled=True,
                    )
                    for u in updates
                )
            )

        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        # Both observed outcome keys land on the shared transaction.
        assert calls.get("apple_music.persistent_lookup.cache_hit") is True
        assert calls.get("apple_music.persistent_lookup.live_error") is True
        # The unobserved outcomes stay absent.
        for unobserved in _RESOLVE_SOURCE_VALUES - {"cache_hit", "live_error"}:
            assert f"apple_music.persistent_lookup.{unobserved}" not in calls
        # The duplicate cache_hit invocation re-set the key idempotently.
        cache_hit_set_calls = [
            c
            for c in transaction.set_data.call_args_list
            if c.args[0] == "apple_music.persistent_lookup.cache_hit"
        ]
        assert len(cache_hit_set_calls) == 2
        assert all(c.args[1] is True for c in cache_hit_set_calls)
