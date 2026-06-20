-- Streaming-URL cache schema for LML#573.
--
-- This file is the canonical DDL reference for the polymorphic
-- `lml_cache.album_streaming_url_cache` table. Unlike `entity.release_identity`
-- (the discogs-cache-owned identity contract, migrated via alembic), this table
-- lives in the LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288, Option
-- 3) and is bootstrapped from LML's own FastAPI lifespan — no discogs-cache
-- coordination. discogs-cache tooling never touches `lml_cache.*`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/streaming_url_cache.py`
-- (`set_up_streaming_url_cache_schema`), which issues these statements via the
-- `IF NOT EXISTS` forms on every boot. If the canonical shape changes, update
-- both places (and the parity assertions in
-- tests/unit/test_streaming_url_cache_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache (
    -- Service discriminator. The named CHECK constraint pins the allowed set
    -- so a typo'd service key fails loudly at write time rather than minting a
    -- silently-unreadable row. PR-3 added 'bandcamp'; a new service is added by
    -- extending this list AND the idempotent ALTER below (the runtime drives
    -- both from `_SERVICES`).
    service TEXT NOT NULL,
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    -- NULL `url` records a known miss; `last_checked_at` gates its TTL
    -- (LML#576 staleness is a SQL-side filter). A non-null `url` is a durable
    -- hit that never goes stale.
    url TEXT,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized),
    CONSTRAINT album_streaming_url_cache_service_valid CHECK (
        service IN ('apple_music_album', 'spotify_album', 'bandcamp')
    )
);

-- Idempotent widen of the named CHECK so an already-created table (where
-- `CREATE TABLE IF NOT EXISTS` is a no-op) picks up service values added after
-- its creation. Byte-stable across boots; runs atomically within the one
-- statement. The runtime (`set_up_streaming_url_cache_schema`) issues this on
-- every boot, generated from `_SERVICES`.
ALTER TABLE lml_cache.album_streaming_url_cache
    DROP CONSTRAINT IF EXISTS album_streaming_url_cache_service_valid,
    ADD CONSTRAINT album_streaming_url_cache_service_valid CHECK (
        service IN ('apple_music_album', 'spotify_album', 'bandcamp')
    );
