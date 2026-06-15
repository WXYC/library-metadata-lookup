"""Post-process step in ``/api/v1/lookup`` that guarantees a non-null
``apple_music_url`` (cache + live probe) for every requested album,
regardless of the album's status in ``library.db``.

Called by ``lookup/orchestrator.py::enrich_one`` after the existing
per-item enrichment has assembled the ``update`` dict. When the dict's
``apple_music_url`` came out null AND the feature flag is on AND we have
an Apple Music client, a discogs-cache ``PgSource`` (for the cache layer),
and an entity store (for the mint side-effect), AND the request supplied
both artist and album, this module:

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
3. Mints the parsed album_id into ``entity.release_identity.apple_music_album_id``
   on ``live_resolved`` outcomes only. Cache hits already minted on
   their original resolution; re-minting writes redundant
   reconciliation log rows for no benefit. The mint is best-effort —
   ``mint_or_get_release_identity`` failures (PG outage, validation
   rejection, unparseable URL) are logged and swallowed so the
   user-visible URL still surfaces in the response.
4. Tags two booleans on the active Sentry transaction:

   * ``apple_music.persistent_lookup.fired = True`` — "post-process ran
     at least once on this request" (back-compat for any dashboard that
     already keys off this signal).
   * ``apple_music.persistent_lookup.<source> = True`` (LML#575) where
     ``<source>`` is the ``ResolveOutcome.source`` returned by the cache
     layer: ``cache_hit``, ``cache_miss_recent``, ``live_resolved``,
     ``live_miss``, or ``live_error``. Each invocation sets one outcome
     key; across the ``enrich_one`` gather fan-out, multiple keys can
     land on the same transaction (one per distinct outcome observed).
     Race-free because different keys never overwrite each other, and
     the same key set to ``True`` repeatedly is idempotent. The
     cardinality budget is 5 boolean keys, well under Sentry's per-span
     attribute ceiling. Dashboards can query the cache hit rate, live
     error rate (Apple outage signal), and total firings without
     scraping per-item logs.

Side effects degrade gracefully. PG errors swallow inside the cache
layer; the live probe is wrapped in ``asyncio.wait_for`` so a single
Apple 429/5xx storm cannot pin the request past its budget; mint
failures log and continue; Sentry projection errors log and continue.
The response shape is unaffected by any observability or persistence
failure.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import ResolveSource, resolve_apple_music_url_with_cache
from entity.sources import PgSource
from entity.store import EntityStore
from identity.release_validation import validate_and_canonicalize_external_id
from lookup.timeouts import apple_music_lookup_timeout_s
from release.apple_music_url_parser import apple_album_id_from_url

logger = logging.getLogger(__name__)


async def apply_apple_music_postprocess(
    update: dict[str, Any],
    *,
    apple_music: AppleMusicClient | None,
    pg: PgSource | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    feature_enabled: bool,
) -> None:
    """Backstop ``update["apple_music_url"]`` via cache + live probe.

    Mutates ``update`` in place. No-op when any precondition fails (flag
    off, URL already set, no client/pg/store, no request artist or album).

    Args:
        update: The item's ``update`` dict from ``enrich_one``. Must
            carry an ``apple_music_url`` key (``None`` or ``str``). On a
            successful resolution this is overwritten with the URL.
        apple_music: The Apple Music client. ``None`` (credentials
            unconfigured) skips the post-process.
        pg: The discogs-cache ``PgSource`` backing the persistent Apple
            Music URL cache (``entity.album_apple_music_lookup_cache``).
            ``None`` skips the post-process; the cache layer has nowhere
            to read or write.
        entity_store: The entity store, used solely for the
            ``apple_music_album`` mint side-effect on ``live_resolved``
            outcomes. ``None`` skips the post-process — the mint is
            structurally part of the contract, so callers without a
            store should not run the cache+probe either.
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
    if apple_music is None or pg is None or entity_store is None:
        return
    if not request_artist or not request_album:
        return

    outcome = await resolve_apple_music_url_with_cache(
        pg,
        apple_music,
        artist=request_artist,
        album=request_album,
        probe_timeout_s=apple_music_lookup_timeout_s(),
    )

    _mark_fired(outcome.source)

    if outcome.url is None:
        return

    update["apple_music_url"] = outcome.url

    # Mint only on a brand-new live resolution. Cache hits already minted
    # on their original resolution; re-minting writes redundant
    # reconciliation log rows for no benefit. The mint is best-effort —
    # a PG error, validation rejection, or unparseable URL here must NOT
    # undo the user-visible URL surfacing above.
    if outcome.source != "live_resolved":
        return

    album_id = apple_album_id_from_url(outcome.url)
    if album_id is None:
        # Apple returned a URL we can't parse for an album_id (slug-only,
        # malformed locale, novel format). Surface the URL but don't
        # mint — keying the entity graph on an unparseable ID would
        # corrupt downstream joins.
        return

    # Run the sentinel rule before the mint. ``mint_or_get_release_identity``
    # documents that callers must validate first (the public endpoint at
    # ``identity/router.py`` does this); the post-process is the second
    # caller and must honor the same contract. The URL parser's ``\d{6,}``
    # floor is looser than the validator's ``[1-9][0-9]*`` rule (the parser
    # admits all-zero / leading-zero IDs the validator rejects), so this
    # is real defense-in-depth, not a coincidental no-op. Pulled inside
    # the try/except so a validation rejection is treated the same way
    # as a PG outage — log + continue; URL still surfaces.
    try:
        canonical_album_id = validate_and_canonicalize_external_id("apple_music_album", album_id)
        await entity_store.mint_or_get_release_identity(
            source="apple_music_album", external_id=canonical_album_id
        )
    except Exception:
        logger.exception(
            "apple_music_album mint failed for album_id=%s (url=%s) — URL still surfaced",
            album_id,
            outcome.url,
        )


def _mark_fired(source: ResolveSource) -> None:
    """Project the per-request Sentry attributes for the post-process.

    Two booleans land on the active transaction (LML#575):

    * ``apple_music.persistent_lookup.fired = True`` — back-compat
      "post-process ran at least once" signal.
    * ``apple_music.persistent_lookup.<source> = True`` — per-outcome
      boolean keyed by the cache layer's ``ResolveOutcome.source``
      enum (``cache_hit``, ``cache_miss_recent``, ``live_resolved``,
      ``live_miss``, ``live_error``).

    Both keys are set idempotently per item, so concurrent ``enrich_one``
    tasks writing the same key with the same value are race-free.
    Observability must not break the request — every failure mode is
    logged and swallowed.
    """
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("apple_music.persistent_lookup.fired", True)
            scope.transaction.set_data(f"apple_music.persistent_lookup.{source}", True)
    except Exception as e:
        logger.warning(
            "Failed to project apple_music.persistent_lookup tags onto Sentry transaction: %s",
            e,
        )
