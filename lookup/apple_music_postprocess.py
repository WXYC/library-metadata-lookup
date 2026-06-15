"""Post-process step in ``/api/v1/lookup`` that guarantees a non-null
``apple_music_url`` (cache + live probe) for every requested album,
regardless of the album's status in ``library.db``.

Called by ``lookup/orchestrator.py::enrich_one`` after the existing
per-item enrichment has assembled the ``update`` dict. When the dict's
``apple_music_url`` came out null AND the feature flag is on AND we have
both an Apple Music client and an entity store with PG access AND the
request supplied both artist and album, this module:

1. Runs ``entity.apple_music_album_cache.resolve_apple_music_url_with_cache``
   with the REQUEST's ``(artist, album, song)`` — not the library row's.
   This is the fix for the wrong-fallback-row attack where a non-library
   album (Hyd / "Hold Onto Me Infinity") gets matched to a same-titled
   library row by a different artist ("Angel" by "Angel"), and the
   in-line probe runs with the wrong artist name.
2. Mutates the ``update`` dict in place when the resolver returns a URL.
3. Mints the parsed album_id into ``entity.release_identity`` so the
   entity graph stays current — only on ``live_resolved`` (cache hits
   were already minted on their original resolution).
4. Tags ``apple_music.persistent_lookup.source`` on the active Sentry
   transaction so dashboards can quantify the cache hit rate.

All side effects degrade gracefully. PG errors swallow inside the cache
layer; mint failures log and continue; Sentry projection errors log and
continue. The request's response shape is unaffected by any
observability or persistence failure.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import (
    ResolveOutcome,
    resolve_apple_music_url_with_cache,
)
from entity.store import EntityStore
from release.apple_music_url_parser import apple_album_id_from_url

logger = logging.getLogger(__name__)


async def apply_apple_music_postprocess(
    update: dict[str, Any],
    *,
    apple_music: AppleMusicClient | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    request_song: str | None,
    feature_enabled: bool,
) -> None:
    """Backstop ``update["apple_music_url"]`` via cache + live probe.

    Mutates ``update`` in place. No-op when any precondition fails (flag
    off, URL already set, no client/store/PG, no request artist or album).

    Args:
        update: The item's ``update`` dict from ``enrich_one``. Must
            carry an ``apple_music_url`` key (None or str). On a
            successful resolution this is overwritten with the URL.
        apple_music: The Apple Music client. ``None`` (credentials
            unconfigured) skips the post-process.
        entity_store: The entity store; required for the cache PG handle
            and the mint side-effect. ``None`` skips the post-process.
        request_artist: The lookup request's artist (not the library
            row's artist — that's the whole point).
        request_album: The lookup request's album. ``None``/empty skips.
        request_song: The lookup request's song. ``None`` is fine; the
            probe falls back to artist+album matching.
        feature_enabled: ``LML_PERSIST_APPLE_MUSIC_URL`` flag. ``False``
            skips.
    """
    if not feature_enabled:
        return
    if update.get("apple_music_url"):
        return
    if apple_music is None or entity_store is None:
        return
    if not request_artist or not request_album:
        return

    outcome = await resolve_apple_music_url_with_cache(
        entity_store.pg,
        apple_music,
        artist=request_artist,
        album=request_album,
        song=request_song,
    )

    _set_sentry_source(outcome.source)

    if outcome.url is None:
        return

    update["apple_music_url"] = outcome.url

    # Mint only on a brand-new live resolution. Cache hits already minted
    # on their original resolution; re-minting writes redundant
    # reconciliation log rows for no benefit. The mint is best-effort —
    # a PG error here must not undo the user-visible URL surfacing.
    if outcome.source != "live_resolved":
        return

    album_id = apple_album_id_from_url(outcome.url)
    if album_id is None:
        # Apple returned a URL we can't parse for an album_id (slug-only,
        # malformed locale, novel format). Surface the URL but don't
        # mint — keying the entity graph on an unparseable ID would
        # corrupt downstream joins.
        return

    try:
        await entity_store.mint_or_get_release_identity(
            source="apple_music_album", external_id=album_id
        )
    except Exception:
        logger.exception(
            "apple_music_album mint failed for album_id=%s (url=%s) — URL still surfaced",
            album_id,
            outcome.url,
        )


def _set_sentry_source(source: ResolveOutcome) -> None:
    """Project ``apple_music.persistent_lookup.source`` onto the active
    Sentry transaction. Observability must not break the request — every
    failure mode is logged and swallowed."""
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("apple_music.persistent_lookup.source", source)
    except Exception as e:
        logger.warning(
            "Failed to project apple_music.persistent_lookup.source onto Sentry transaction: %s",
            e,
        )
