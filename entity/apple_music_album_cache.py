"""Persistent (artist, album) -> Apple Music URL cache.

LML's ``/api/v1/lookup`` resolves Apple Music URLs by probing Apple's
catalog with the request's (artist, album, song). This module caches each
resolution so subsequent lookups for the same album hit a row in
PostgreSQL rather than re-querying Apple. The cache is the durable
source of truth for non-library albums (no row in ``library.db``) —
``library.db.streaming_links`` covers librarian-curated overrides, this
covers everything else.

Keys: ``(artist_normalized, album_normalized)``, where normalization
applies ``wxyc_etl.text.to_match_form`` (NFC + diacritic stripping +
lowercasing + whitespace collapse). The cache module is the single owner
of normalization; callers pass raw request strings.

Hit/miss semantics (LML#576: staleness is now a SQL-side filter, mirroring
``discogs/cache_service.py::lookup_negative_hit``):

* ``apple_music_url IS NOT NULL`` — durable hit. Hits never expire; the
  ``last_checked_at`` column is informational. Manual eviction is a
  single ``DELETE`` if a URL goes bad.
* ``apple_music_url IS NULL`` AND ``last_checked_at > now() - miss_ttl`` —
  known miss inside the TTL window. The SELECT returns this row; the
  caller short-circuits and skips the Apple call.
* ``apple_music_url IS NULL`` AND ``last_checked_at <= now() - miss_ttl`` —
  stale miss. The SELECT filter excludes the row, so callers see the same
  "no row" shape as an absent entry and fall through to the live probe.
  The stale row stays in the table and the next live probe's UPSERT
  refreshes it in place.

PG failures degrade to a no-op: ``get`` returns ``None``, ``set``
swallows the exception. The caller (``lookup/orchestrator.py``) then
falls through to the live Apple Music probe as if no cache were
configured. Schema bootstrap (``set_up_apple_music_cache_schema``) is
called from ``main.py`` lifespan; the DDL is ``IF NOT EXISTS`` so
re-running on every boot is safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from wxyc_etl.text import to_match_form

from clients.streaming.apple_music import AppleMusicClient
from entity.sources import PgSource

logger = logging.getLogger(__name__)

# How long a known-miss row stays authoritative before we re-check Apple.
# Hits are kept forever; this TTL only applies when ``apple_music_url IS NULL``.
# 7 days trades freshness for cache amortization — long enough that bulk
# request-spike traffic doesn't repeatedly hammer Apple for the same misses,
# short enough that a newly-released album becomes visible within a week.
DEFAULT_MISS_TTL = timedelta(days=7)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS entity"

_DDL = """\
CREATE TABLE IF NOT EXISTS entity.album_apple_music_lookup_cache (
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    apple_music_url TEXT,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_normalized, album_normalized)
)\
"""

# Staleness lives in the WHERE clause (LML#576). ``$3`` is the lower
# bound on ``last_checked_at`` for a "fresh" known miss — typically
# ``now - miss_ttl`` computed by the caller. A hit (URL not null) is
# always returned; a stale miss is filtered out and the caller sees
# the same "no row" shape as an absent entry.
_SELECT_SQL = """\
SELECT apple_music_url
FROM entity.album_apple_music_lookup_cache
WHERE artist_normalized = $1 AND album_normalized = $2
  AND (apple_music_url IS NOT NULL OR last_checked_at > $3)\
"""

# UPSERT: keep the latest URL and refresh ``last_checked_at`` on conflict.
# The ``url=None`` write path explicitly records "checked and not found at
# time T" so subsequent lookups can short-circuit Apple inside the TTL.
_UPSERT_SQL = """\
INSERT INTO entity.album_apple_music_lookup_cache
    (artist_normalized, album_normalized, apple_music_url, last_checked_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (artist_normalized, album_normalized) DO UPDATE
SET apple_music_url = EXCLUDED.apple_music_url,
    last_checked_at = EXCLUDED.last_checked_at\
"""


async def set_up_apple_music_cache_schema(pg: PgSource) -> None:
    """Apply the idempotent DDL.

    Called once from ``main.py`` lifespan. The schema and table creation
    are both ``IF NOT EXISTS`` so re-running on every boot is safe. The
    schema-creation step lets a fresh discogs-cache PG (local dev via
    ``setup-dev-environment.sh``, test fixtures, future docker-compose
    stacks) boot LML cleanly without first running the discogs-cache
    repo's own schema setup. If the discogs-cache PG is unreachable at
    startup, the caller logs and continues; the cache layer degrades to
    a no-op until the next deploy.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL)


@dataclass(frozen=True)
class _CachedRow:
    """Internal carrier for a cache SELECT outcome.

    The cache's public surface is ``str | None`` — the resolver only needs
    the URL. ``_CachedRow`` carries the extra "was a row returned" bit
    that the resolver uses to distinguish a fresh known miss (skip Apple)
    from a stale or absent entry (call Apple). Kept private to this
    module so its shape can evolve without leaking into call sites.
    """

    url: str | None
    was_present: bool


async def get_cached_apple_music_url(
    pg: PgSource,
    *,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    now: datetime | None = None,
) -> str | None:
    """Look up a previously resolved Apple Music URL for ``(artist, album)``.

    Returns the cached URL, or ``None`` for absent / stale / not-found rows.
    The SQL ``WHERE`` clause excludes stale known-misses (rows whose
    ``apple_music_url IS NULL`` and ``last_checked_at`` is past the
    ``miss_ttl`` window) so callers can't tell a stale miss from an absent
    entry — both render as ``None`` and the caller falls through to a
    live probe.

    PG failures return ``None`` (same posture as the discogs negative-cache
    helper); the caller treats it as a miss and falls through to the
    live probe.

    ``now`` is exposed for testability (no freezegun dependency); callers
    in production should leave it at the default and let the function
    compute ``datetime.now(timezone.utc)`` itself.
    """
    result = await _fetch_cached_row(pg, artist=artist, album=album, miss_ttl=miss_ttl, now=now)
    return result.url


async def _fetch_cached_row(
    pg: PgSource,
    *,
    artist: str,
    album: str,
    miss_ttl: timedelta,
    now: datetime | None,
) -> _CachedRow:
    """Read the cache row keyed by ``(artist, album)`` honoring the TTL filter.

    Module-private helper that powers both ``get_cached_apple_music_url``
    (which returns just the URL) and ``resolve_apple_music_url_with_cache``
    (which needs ``was_present`` to distinguish a fresh known miss from a
    stale/absent row before deciding whether to call Apple).
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    reference_now = now or datetime.now(UTC)
    miss_cutoff = reference_now - miss_ttl
    try:
        row = await pg.fetchone(_SELECT_SQL, artist_key, album_key, miss_cutoff)
    except Exception:
        logger.exception("apple_music_album_cache get failed for %s / %s", artist_key, album_key)
        return _CachedRow(url=None, was_present=False)

    if row is None:
        # Either no row at all, or a stale known-miss filtered out by the
        # SQL WHERE. Both render the same way to callers; the resolver
        # treats either as "call Apple."
        return _CachedRow(url=None, was_present=False)

    return _CachedRow(url=row["apple_music_url"], was_present=True)


ResolveSource = Literal[
    "cache_hit",
    "cache_miss_recent",
    "live_resolved",
    "live_miss",
    "live_error",
]


@dataclass(frozen=True)
class ResolveOutcome:
    """Result of the cache-backed Apple Music URL resolution.

    Carries both the URL and the path taken so callers can tag Sentry without
    reproducing the resolution's branch logic. ``url is None`` whenever
    ``source`` is ``"cache_miss_recent"``, ``"live_miss"``, or
    ``"live_error"`` — but ``live_error`` is a distinct signal so dashboards
    can tell a genuine Apple-catalog miss from an upstream outage.
    """

    url: str | None
    source: ResolveSource


async def resolve_apple_music_url_with_cache(
    pg: PgSource,
    apple_music: AppleMusicClient,
    *,
    artist: str,
    album: str,
    probe_timeout_s: float | None = None,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    now: datetime | None = None,
) -> ResolveOutcome:
    """Read-through cache around ``apple_music.find_album_match``.

    Branch order:

    1. Cache row exists with a non-null URL → return ``cache_hit`` (no API).
    2. Cache row exists with a NULL URL and ``last_checked_at`` inside
       ``miss_ttl`` → return ``cache_miss_recent`` (no API).
    3. Otherwise call ``apple_music.find_album_match(artist, album)`` and
       record the outcome:
       - Apple returns a match → ``live_resolved`` and UPSERT the URL.
       - Apple returns ``None`` → ``live_miss`` and UPSERT a null entry so
         subsequent requests inside the TTL short-circuit.
       - Apple raises (or the probe times out) → ``live_error`` with NO
         cache write so a transient flake doesn't lock in a spurious null,
         and Sentry can tell the outage apart from a genuine catalog miss.

    Uses ``find_album_match`` rather than ``find_track_metadata`` because
    the cache key is the album, the cache table name is album-shaped, and
    Apple's song-search returns per-track deep-links (``…?i=<track-id>``)
    that would cache wrong-track URLs against the (artist, album) key on
    every song-search request. ``find_album_match`` returns a SourceMatch
    whose ``url`` is the album page.

    The live probe is wrapped in ``asyncio.wait_for`` with
    ``probe_timeout_s`` (caller supplies; the orchestrator passes
    ``LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS / 1000``) so a single Apple
    429/5xx storm cannot pin a request past its budget — same bound as
    the in-line probe at ``lookup/orchestrator.py:2336-2346``.
    """
    cached = await _fetch_cached_row(pg, artist=artist, album=album, miss_ttl=miss_ttl, now=now)
    if cached.url is not None:
        return ResolveOutcome(url=cached.url, source="cache_hit")
    if cached.was_present:
        # SQL filter already excluded stale misses; a present row with
        # NULL URL is necessarily an in-TTL known miss — skip Apple.
        return ResolveOutcome(url=None, source="cache_miss_recent")

    # Cache empty or stale — call Apple with REQUEST values, wrapped in
    # the same per-call wall-clock ceiling the in-line probe uses.
    try:
        probe = apple_music.find_album_match(artist, album)
        if probe_timeout_s is not None:
            match = await asyncio.wait_for(probe, timeout=probe_timeout_s)
        else:
            match = await probe
    except TimeoutError:
        # Timed out — same posture as in-line probe (LML#449/#450): do not
        # poison the cache; surface as live_error so dashboards distinguish.
        logger.warning("apple_music_album_cache live probe timed out for %s / %s", artist, album)
        return ResolveOutcome(url=None, source="live_error")
    except Exception:
        # Transient Apple-side failure: do not poison the cache with a
        # permanent "not found" sentinel — leave the row alone and let the
        # next request retry.
        logger.exception("apple_music_album_cache live probe raised for %s / %s", artist, album)
        return ResolveOutcome(url=None, source="live_error")

    if match is None:
        await set_cached_apple_music_url(pg, artist=artist, album=album, url=None)
        return ResolveOutcome(url=None, source="live_miss")

    await set_cached_apple_music_url(pg, artist=artist, album=album, url=match.url)
    return ResolveOutcome(url=match.url, source="live_resolved")


async def set_cached_apple_music_url(
    pg: PgSource,
    *,
    artist: str,
    album: str,
    url: str | None,
) -> None:
    """UPSERT a cache row for ``(artist, album)``.

    Pass ``url`` for a hit, ``None`` to record a known miss. The
    ``last_checked_at`` column is set to ``now()`` server-side on both
    insert and conflict-update, so consumers see one consistent timestamp.

    Write failures are logged and swallowed: cache writes are best-effort.
    A request that produced a real Apple Music URL still returns it to the
    caller even if the cache write fails.
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    try:
        await pg.execute(_UPSERT_SQL, artist_key, album_key, url)
    except Exception:
        logger.exception("apple_music_album_cache set failed for %s / %s", artist_key, album_key)
