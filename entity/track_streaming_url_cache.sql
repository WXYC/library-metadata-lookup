-- Track-scoped streaming-URL cache schema for LML#893 (lever L1).
--
-- Canonical DDL reference for the `lml_cache.track_streaming_url_cache` table.
-- Like `lml_cache.album_streaming_url_cache`, this table lives in the LML-owned
-- `lml_cache.*` schema (per WXYC/discogs-etl#288, Option 3) and is bootstrapped
-- from LML's own FastAPI lifespan — no discogs-cache coordination.
--
-- It is a SEPARATE table from the album cache on purpose (see the closed PR
-- #898 poisoning bug): it carries a real `song_normalized` axis in the PK so a
-- per-track Apple Music deep-link is keyed to the played track and a DIFFERENT
-- song of the same album is a MISS. The runtime never writes track deep-links
-- into `album_streaming_url_cache`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/track_streaming_url_cache.py`
-- (`set_up_track_streaming_url_cache_schema`), which issues these statements via
-- the `IF NOT EXISTS` forms on every boot. If the canonical shape changes,
-- update both places (and the parity assertions in
-- tests/unit/test_track_streaming_url_cache_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.track_streaming_url_cache (
    -- Service discriminator. The named CHECK constraint pins the allowed set so
    -- a typo'd service key fails loudly at write time. A new service is added by
    -- extending this list AND the idempotent ALTER below (the runtime drives
    -- both from `_SERVICES`).
    service TEXT NOT NULL,
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    -- The song axis: what makes this table track-granular. A different song of
    -- the same album keys to a different row (a MISS), so a cached hit always
    -- deep-links to the played track.
    song_normalized TEXT NOT NULL,
    -- `url` is NOT NULL: this cache stores only resolved track deep-links, never
    -- a "checked, not found" sentinel. That structurally enforces the
    -- #782/BS#1192 guard — a null can never be persisted on a first lookup.
    url TEXT NOT NULL,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized, song_normalized),
    CONSTRAINT track_streaming_url_cache_service_valid CHECK (
        service IN ('apple_music_track')
    )
);

-- Idempotent widen of the named CHECK so an already-created table (where
-- `CREATE TABLE IF NOT EXISTS` is a no-op) picks up service values added after
-- its creation. Byte-stable across boots; runs atomically within the one
-- statement. The runtime (`set_up_track_streaming_url_cache_schema`) issues this
-- on every boot, generated from `_SERVICES`.
ALTER TABLE lml_cache.track_streaming_url_cache
    DROP CONSTRAINT IF EXISTS track_streaming_url_cache_service_valid,
    ADD CONSTRAINT track_streaming_url_cache_service_valid CHECK (
        service IN ('apple_music_track')
    );
