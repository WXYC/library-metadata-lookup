"""Persistent (service, artist, album, song) -> track-URL cache (LML#893, L1).

LML's ``/api/v1/lookup`` happy path (an acceptable library row) probes Apple
Music with ``find_track_url`` to surface the *played track's* deep-link — a
~4.8s synchronous call on the hot path. This module caches that per-track URL
so a repeat lookup of the same ``(artist, album, played-track)`` skips the live
probe and returns the cached exact-track deep-link.

**Why a SEPARATE table from ``album_streaming_url_cache``.** The album cache
(``entity/streaming_url_cache.py``) is keyed on ``(service, artist, album)`` —
no song axis — and is filled with *album* URLs by the streaming post-process.
Writing a per-track deep-link into it (the closed PR #898 approach) poisoned it:
wrong-track URLs on repeat lookups, permanent cross-seam contamination, and
warm-vs-cold divergence on the public ``apple_music_url`` field. The played
track needs a track-granular URL, so L1 gets its own table with a real song
axis in the PK. **This module never touches the album cache.**

Keys: ``(service, artist_normalized, album_normalized, song_normalized)`` where
normalization applies ``wxyc_etl.text.to_match_form`` (NFC + diacritic
stripping + lowercasing + whitespace collapse). A ``None`` album normalizes to
the empty string so album-absent lookups still key stably.

Hit/miss semantics are hit-only. Unlike the album cache, this table records NO
known-miss rows: the ``url`` column is ``NOT NULL`` so a null can never be
persisted (the #782 / BS#1192 "don't persist a null on first lookup" guard is
enforced at the schema level, not just by the caller). A SELECT returns the
durable hit or nothing.

PG failures degrade to a no-op: ``get`` returns ``None``, ``set`` swallows the
exception. The caller (``lookup/enrichment/item.py``) then falls through to the
live probe as if no cache were configured. Schema bootstrap
(``set_up_track_streaming_url_cache_schema``) is called from ``main.py``
lifespan; the DDL is ``IF NOT EXISTS`` so re-running on every boot is safe.
"""

from __future__ import annotations

import logging

from wxyc_etl.text import to_match_form

from entity.sources import PgSource

logger = logging.getLogger(__name__)

# The single service L1 ships. The named CHECK constraint pins this set at the
# DB level; a future PR (e.g. a Spotify track deep-link) extends this tuple and
# the ALTER below picks it up. Kept as a module constant so the bootstrap DDL
# and the parity test can both reference it without re-typing the literal.
APPLE_MUSIC_TRACK_SERVICE = "apple_music_track"
_SERVICES = (APPLE_MUSIC_TRACK_SERVICE,)

# Shared IN-list literal so the CREATE-time CHECK and the idempotent ALTER are
# generated from the same source — they can never drift.
_SERVICE_IN_LIST = ", ".join(f"'{s}'" for s in _SERVICES)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

# ``url TEXT NOT NULL`` enforces the #782/BS#1192 guard structurally: this
# cache stores only resolved track deep-links, never a "checked, not found"
# sentinel. The named CHECK constraint uses a name distinct from the album
# cache's so the two tables' idempotent ALTERs never collide.
_DDL_TABLE = f"""\
CREATE TABLE IF NOT EXISTS lml_cache.track_streaming_url_cache (
    service TEXT NOT NULL,
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    song_normalized TEXT NOT NULL,
    url TEXT NOT NULL,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized, song_normalized),
    CONSTRAINT track_streaming_url_cache_service_valid CHECK (
        service IN ({_SERVICE_IN_LIST})
    )
)\
"""

# Idempotent widen of the named CHECK. ``CREATE TABLE IF NOT EXISTS`` is a
# no-op on an already-created prod table, so a new service value added to
# ``_SERVICES`` would never reach an existing table without this ALTER. The
# DROP-IF-EXISTS + ADD pair is byte-stable across boots and runs atomically
# within the single statement. Driven from the same ``_SERVICE_IN_LIST`` as the
# CREATE so the two can't drift.
_DDL_ALTER_CHECK = f"""\
ALTER TABLE lml_cache.track_streaming_url_cache
    DROP CONSTRAINT IF EXISTS track_streaming_url_cache_service_valid,
    ADD CONSTRAINT track_streaming_url_cache_service_valid CHECK (
        service IN ({_SERVICE_IN_LIST})
    )\
"""

# Hit-only SELECT: the ``url NOT NULL`` schema means any present row is a
# durable hit. Keyed on the full ``(service, artist, album, song)`` composite —
# ``song_normalized`` is the axis that makes a DIFFERENT song of the same album
# a MISS.
_SELECT_SQL = """\
SELECT url
FROM lml_cache.track_streaming_url_cache
WHERE service = $1 AND artist_normalized = $2
  AND album_normalized = $3 AND song_normalized = $4\
"""

# UPSERT: keep the latest URL and refresh ``last_checked_at`` on conflict.
_UPSERT_SQL = """\
INSERT INTO lml_cache.track_streaming_url_cache
    (service, artist_normalized, album_normalized, song_normalized, url, last_checked_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (service, artist_normalized, album_normalized, song_normalized) DO UPDATE
SET url = EXCLUDED.url,
    last_checked_at = EXCLUDED.last_checked_at\
"""


async def set_up_track_streaming_url_cache_schema(pg: PgSource) -> None:
    """Apply the idempotent track-cache-schema DDL.

    Called once from ``main.py`` lifespan. Schema and table creation are both
    ``IF NOT EXISTS`` so re-running on every boot is safe. The final ALTER
    widens the named CHECK to the current ``_SERVICES`` set so a prod table
    frozen at an older service set still accepts a newly-added service.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)
    await pg.execute(_DDL_ALTER_CHECK)


def _album_key(album: str | None) -> str:
    """Normalize the album key; ``None`` -> ``""`` for album-absent lookups."""
    return to_match_form(album) if album else ""


async def get_cached_track_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str | None,
    song: str,
) -> str | None:
    """Return the cached track deep-link for ``(service, artist, album, song)``.

    Returns the URL on a hit, or ``None`` when no row exists. Keyed on the song
    so a DIFFERENT song of the same album is a miss. PG failures return
    ``None`` (same posture as the album cache's negative-cache helper).
    """
    artist_key = to_match_form(artist)
    album_key = _album_key(album)
    song_key = to_match_form(song)
    try:
        row = await pg.fetchone(_SELECT_SQL, service, artist_key, album_key, song_key)
    except Exception:
        logger.exception(
            "track_streaming_url_cache get failed for %s / %s / %s / %s",
            service,
            artist_key,
            album_key,
            song_key,
        )
        return None
    if row is None:
        return None
    return row["url"]


async def set_cached_track_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str | None,
    song: str,
    url: str,
) -> None:
    """UPSERT a resolved track deep-link for ``(service, artist, album, song)``.

    Only non-null URLs are ever written — the schema's ``url NOT NULL`` column
    makes persisting a null impossible, enforcing the #782/BS#1192 guard.

    Write failures are logged and swallowed: cache writes are best-effort. A
    request that produced a real URL still returns it even if the write fails.
    """
    artist_key = to_match_form(artist)
    album_key = _album_key(album)
    song_key = to_match_form(song)
    try:
        await pg.execute(_UPSERT_SQL, service, artist_key, album_key, song_key, url)
    except Exception:
        logger.exception(
            "track_streaming_url_cache set failed for %s / %s / %s / %s",
            service,
            artist_key,
            album_key,
            song_key,
        )
