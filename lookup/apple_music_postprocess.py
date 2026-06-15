"""Post-process step in ``/api/v1/lookup`` that guarantees a non-null
``apple_music_url`` (cache + live probe) for every requested album,
regardless of the album's status in ``library.db``.

Called by ``lookup/orchestrator.py::enrich_one`` after the existing
per-item enrichment has assembled the ``update`` dict. When the dict's
``apple_music_url`` came out null AND the feature flag is on AND we have
both an Apple Music client and an entity store with PG access AND the
request supplied both artist and album, this module:

1. Runs ``entity.apple_music_album_cache.resolve_apple_music_url_with_cache``
   with the REQUEST's ``(artist, album)`` — not the library row's. This
   is the fix for the wrong-fallback-row attack where a non-library
   album (Hyd / "Hold Onto Me Infinity") gets matched to a same-titled
   library row by a different artist ("Angel" by "Angel"), and the
   in-line probe runs with the wrong artist name. The resolver uses
   ``find_album_match`` (album-level), not ``find_track_metadata``, so
   the cache stores album URLs that are reused across different song
   lookups on the same album.
2. Mutates the ``update`` dict in place when the resolver returns a URL.
3. Tags ``apple_music.persistent_lookup.fired = True`` on the active
   Sentry transaction. The per-item outcome is intentionally NOT tagged
   on the transaction because ``enrich_one`` runs concurrently across
   N items via ``asyncio.gather``; per-item ``set_data`` on the same key
   would clobber itself N-1 times and the dashboard would see a
   non-deterministic last-completer-wins value. The cache layer's own
   logging is the per-call signal; the transaction-level boolean tells
   dashboards "this request exercised the post-process at least once."

Side effects degrade gracefully. PG errors swallow inside the cache
layer; the live probe is wrapped in ``asyncio.wait_for`` so a single
Apple 429/5xx storm cannot pin the request past its budget; Sentry
projection errors log and continue. The response shape is unaffected
by any observability or persistence failure.

**Mint is intentionally not invoked here.** Wiring ``apple_music_album``
through ``identity.release_validation.RELEASE_SOURCE_COLUMN`` +
``coerce_external_id`` is a separate change — calling
``mint_or_get_release_identity`` today would raise ``KeyError`` on
every successful resolution and silently log via the surrounding
``except`` block. The cache-write side persists the URL; the
release-identity hop is filed as a follow-up.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import sentry_sdk

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import resolve_apple_music_url_with_cache
from entity.store import EntityStore

logger = logging.getLogger(__name__)

_APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S = 4.0
_APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR = "LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS"


def _probe_timeout_s() -> float:
    """Per-call wall-clock ceiling for the cache-backed Apple probe.

    Mirrors the in-line probe's helper at
    ``lookup/orchestrator.py::_apple_music_lookup_timeout_s`` so a single
    Apple 429/5xx storm can't pin the post-process past the request
    budget. Read at request time (not via ``Settings``) so the knob can
    be tuned via Railway env vars without a redeploy. A misconfigured
    value (negative, zero, unparseable) falls back to 4s with a WARN —
    same fallback semantics the orchestrator's helper has.
    """
    raw = os.getenv(_APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR)
    if not raw:
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    try:
        ms = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to %.1fs",
            _APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
            raw,
            _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S,
        )
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    if ms <= 0:
        logger.warning(
            "Invalid %s=%d (must be positive), falling back to %.1fs",
            _APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
            ms,
            _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S,
        )
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    return ms / 1000


async def apply_apple_music_postprocess(
    update: dict[str, Any],
    *,
    apple_music: AppleMusicClient | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    feature_enabled: bool,
) -> None:
    """Backstop ``update["apple_music_url"]`` via cache + live probe.

    Mutates ``update`` in place. No-op when any precondition fails (flag
    off, URL already set, no client/store/PG, no request artist or album).

    Args:
        update: The item's ``update`` dict from ``enrich_one``. Must
            carry an ``apple_music_url`` key (``None`` or ``str``). On a
            successful resolution this is overwritten with the URL.
        apple_music: The Apple Music client. ``None`` (credentials
            unconfigured) skips the post-process.
        entity_store: The entity store; required for the cache PG handle.
            ``None`` skips the post-process.
        request_artist: The lookup request's artist (not the library
            row's artist — that's the whole point).
        request_album: The lookup request's album. ``None``/empty skips.
        feature_enabled: ``LML_PERSIST_APPLE_MUSIC_URL`` flag. ``False``
            skips.
    """
    if not feature_enabled:
        return
    # ``is not None`` rather than truthiness: an empty-string sentinel in
    # ``update["apple_music_url"]`` (a legitimate "explicitly checked,
    # nothing to surface" override) must NOT trigger the post-process.
    if update.get("apple_music_url") is not None:
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
        probe_timeout_s=_probe_timeout_s(),
    )

    _mark_fired()

    if outcome.url is None:
        return

    update["apple_music_url"] = outcome.url


def _mark_fired() -> None:
    """Project ``apple_music.persistent_lookup.fired = True`` onto the
    active Sentry transaction. Idempotent — concurrent ``enrich_one``
    tasks setting the same boolean is race-free. Observability must not
    break the request — every failure mode is logged and swallowed.

    The richer per-call signal (``cache_hit`` / ``live_resolved`` / ...)
    is left to the cache layer's logs because a per-item ``set_data``
    on the shared transaction would race the way described in the
    module docstring.
    """
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("apple_music.persistent_lookup.fired", True)
    except Exception as e:
        logger.warning(
            "Failed to project apple_music.persistent_lookup.fired onto Sentry transaction: %s",
            e,
        )
